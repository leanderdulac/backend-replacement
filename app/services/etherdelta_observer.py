from ..app import App
import asyncio
from app.config import ED_CONTRACT_ADDR, ED_CONTRACT_ABI, ED_WS_SERVERS
from ..src.contract_event_utils import block_timestamp
from datetime import datetime
from decimal import Decimal
import json
import logging
from ..src.order_enums import OrderSource, OrderState
from ..src.order_hash import make_order_hash
from pprint import pformat
from queue import Queue, Empty as QueueEmpty
from random import sample
from ..src.socketio_client import SocketIOClient
from time import time
from ..src.utils import parse_insert_status
from web3 import Web3
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

logger = logging.getLogger('etherdelta_observer')
logger.setLevel(logging.DEBUG)

ZERO_ADDR = "0x0000000000000000000000000000000000000000"

CHECK_TOKENS_PER_PONG = 2
market_queue = Queue()
# TODO: Populate from our own DB
with open("tokens.json") as f:
    for token in json.load(f):
        market_queue.put(token["addr"].lower())

web3 = App().web3
contract = web3.eth.contract(ED_CONTRACT_ADDR, abi=ED_CONTRACT_ABI)

async def on_connect(io_client, event):
    logger.info("ED API client connected to %s", io_client.ws_url)

async def on_error(io_client, event, error):
    logger.critical("ED API client (connected to %s) error: %s", io_client.ws_url, error)

async def on_disconnect(io_client, event):
    logger.info("ED API client disconnected from %s", io_client.ws_url)

async def process_orders(orders):
    not_deleted_filter = lambda order: "deleted" not in order or not order["deleted"]
    logger.info("Processing %i orders", len(orders))
    orders = list(filter(not_deleted_filter, orders))
    logger.debug("Filtered orders: %i", len(orders))

    for order in orders:
        try:
            await record_order(order)
        except Exception as e:
            logger.critical("Error while processing order '%s'", order)
            raise e

async def on_orders(io_client, event, payload=None):
    await process_orders([*payload["buys"], *payload["sells"]])

async def on_market(io_client, event, payload):
    if "orders" not in payload:
        # The beautiful market API
        return

    await process_orders([*payload["orders"]["buys"], *payload["orders"]["sells"]])

async def on_pong(io_client, event):
    logger.info("Connection to %s alive: pong received", io_client.ws_url)
    # await io_client.emit("getMarket", { "token": ZERO_ADDR })
    for _ in range(CHECK_TOKENS_PER_PONG):
        try:
            token = market_queue.get()
        except QueueEmpty:
            break # better luck next time!
        else:
            logger.info("Query token %s", token)
            await io_client.emit("getMarket", { "token": token })
            await asyncio.sleep(4)
            market_queue.put(token)

INSERT_ORDER_STMT = """
    INSERT INTO orders
    (
        "source", "signature",
        "token_give", "amount_give", "token_get", "amount_get", "available_volume",
        "expires", "nonce", "user", "state", "v", "r", "s", "date"
    )
    VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, $9, $10, $11, $12, $13, $14)
    ON CONFLICT ON CONSTRAINT index_orders_on_signature DO NOTHING
"""
from ..tasks.update_order import update_order_by_signature
async def record_order(order):
    order_maker = order["user"]
    signature = make_order_hash(order)
    insert_args = (
        OrderSource.OFFCHAIN.name,
        Web3.toBytes(hexstr=signature),
        Web3.toBytes(hexstr=order["tokenGive"]),
        Decimal(order["amountGive"]),
        Web3.toBytes(hexstr=order["tokenGet"]),
        Decimal(order["amountGet"]),
        int(order["expires"]),
        int(order["nonce"]),
        Web3.toBytes(hexstr=order["user"]),
        OrderState.OPEN.name,
        int(order["v"]),
        Web3.toBytes(hexstr=order["r"]),
        Web3.toBytes(hexstr=order["s"]),
        datetime.utcnow()
    )

    async with App().db.acquire_connection() as connection:
        insert_retval = await connection.execute(INSERT_ORDER_STMT, *insert_args)
        _, _, did_insert = parse_insert_status(insert_retval)

    if did_insert:
        logger.info("recorded order signature=%s, user=%s, expires=%i", signature, order["user"], int(order["expires"]))
        update_order_by_signature(signature)

async def main(my_id, num_observers):
    ws_url = ED_WS_SERVERS[my_id]
    io_client = SocketIOClient(ws_url)
    io_client.on("orders", on_orders)
    io_client.on("market", on_market)
    io_client.on("pong", on_pong) # Schedules full refreshes
    io_client.on("connect", on_connect)
    io_client.on("disconnect", on_disconnect)

    last_attempt = None # TODO: Exponential backoff
    while True:
        try:
            await io_client.start()
        except (ConnectionClosed, InvalidStatusCode) as e:
            logger.warn("Connection with %s lost with %s", ws_url, e)
            await asyncio.sleep(5.0)
            continue
    # Exceptions to handle:
    # - websockets.exceptions.InvalidStatusCode: LIKE Status code not 101: 521
    # - websockets.exceptions.ConnectionClosed: LIKE WebSocket connection is closed: code = 1006 (connection closed abnormally [internal]),

NUM_OBSERVERS = 6
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    tasks = [ asyncio.ensure_future(main(i, NUM_OBSERVERS))
                for i in range(0, NUM_OBSERVERS) ]
    loop.run_until_complete(asyncio.gather(*tasks))
