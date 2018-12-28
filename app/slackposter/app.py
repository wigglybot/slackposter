# todo: subscribe to aggregate stream
# todo: discard events which are not of type response
# todo: post an event to dialogue describing result of attempting to post to slack

import asyncio
from photonpump import connect, exceptions
import json
import os
import logging
from slackclient import SlackClient
import functools
from configobj import ConfigObj
from pymongo import MongoClient
import urllib
import requests
import uuid

dir_path = os.path.dirname(os.path.realpath(__file__))
CONFIG = ConfigObj(os.path.join(dir_path, "config.ini"))
ENVIRON = os.getenv("ENVIRON", CONFIG["config"]["ENVIRON"])

EVENT_STORE_URL = os.getenv("EVENT_STORE_URL", CONFIG[ENVIRON]["EVENT_STORE_URL"])
EVENT_STORE_HTTP_PORT = int(os.getenv("EVENT_STORE_HTTP_PORT", CONFIG[ENVIRON]["EVENT_STORE_HTTP_PORT"]))
EVENT_STORE_TCP_PORT = int(os.getenv("EVENT_STORE_TCP_PORT", CONFIG[ENVIRON]["EVENT_STORE_TCP_PORT"]))
EVENT_STORE_USER = os.getenv("EVENT_STORE_USER", CONFIG[ENVIRON]["EVENT_STORE_USER"])
EVENT_STORE_PASS = os.getenv("EVENT_STORE_PASS", CONFIG[ENVIRON]["EVENT_STORE_PASS"])
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", CONFIG[ENVIRON]["SLACK_BOT_TOKEN"])

MONGO_URL = os.getenv("MONGO_URL", CONFIG[ENVIRON]["MONGO_URL"])
MONGO_PORT = int(os.getenv("MONGO_PORT", CONFIG[ENVIRON]["MONGO_PORT"]))
MONGO_USER = urllib.parse.quote_plus(os.getenv("MONGO_USER", CONFIG[ENVIRON]["MONGO_USER"]))
MONGO_PASS = urllib.parse.quote_plus(os.getenv("MONGO_PASS", CONFIG[ENVIRON]["MONGO_PASS"]))

LOGGER_LEVEL = int(os.getenv("LOGGER_LEVEL", CONFIG[ENVIRON]["LOGGER_LEVEL"]))
LOGGER_FORMAT = '%(asctime)s [%(name)s] %(message)s'

V_MA = CONFIG["version"]["MAJOR"]
V_MI = CONFIG["version"]["MINOR"]
V_RE = CONFIG["version"]["REVISION"]
V_DATE = CONFIG["version"]["DATE"]
CODENAME = CONFIG["version"]["CODENAME"]

logging.basicConfig(format=LOGGER_FORMAT, datefmt='[%H:%M:%S]')
log = logging.getLogger("slackposter")

"""
CRITICAL 50
ERROR    40
WARNING  30
INFO     20
DEBUG    10
NOTSET    0
"""
log.setLevel(LOGGER_LEVEL)


def version_fancy():
    return ''.join((
        "\n",
        " (  (                       (         (           )", "\n",
        " )\))(   ' (   (  (  (  (   )\ (    ( )\       ( /(", "\n",
        "((_)()\ )  )\  )\))( )\))( ((_))\ ) )((_)  (   )\())", "\n",
        "_(())\_)()((_)((_))\((_))\  _ (()/(((_)_   )\ (_))/", "\n",
        "\ \((_)/ / (_) (()(_)(()(_)| | )(_))| _ ) ((_)| |_ ",
        "         version: {0}".format("v%s.%s.%s" % (V_MA, V_MI, V_RE)), "\n",
        " \ \/\/ /  | |/ _` |/ _` | | || || || _ \/ _ \|  _|",
        "       code name: {0}".format(CODENAME), "\n",
        "  \_/\_/   |_|\__, |\__, | |_| \_, ||___/\___/ \__|",
        "    release date: {0}".format(V_DATE), "\n",
        "              |___/ |___/      |__/", "\n"
    ))


log.info(version_fancy())


def run_in_executor(f):
    """
    wrap a blocking (non-asyncio) func so it is executed in our loop
    """
    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, functools.partial(f, *args, **kwargs))
    return inner


@run_in_executor
def slackclient_call(_slack_client, text, channel, thread_ts):
    slack_client.api_call(
        "chat.postMessage",
        channel=channel,
        text=text,
        thread_ts=thread_ts
    )


@run_in_executor
def post_to_dialogue_stream(event_id):
    headers = {
        "ES-EventType": "posted",
        "ES-EventId": str(uuid.uuid1())
    }
    requests.post(
        "http://%s:%s/streams/dialogue" % (EVENT_STORE_URL, EVENT_STORE_HTTP_PORT),
        headers=headers,
        json={"event_id": event_id, "posted": True}
    )


@run_in_executor
def fetch_dialogue_object(event):
    event = json.loads(event)
    client = MongoClient('mongodb://%s:%s@%s' % (MONGO_USER, MONGO_PASS, MONGO_URL), MONGO_PORT)
    wigglybot_db = client["wigglybot_db"]
    dialogues = wigglybot_db.db['dialogues']
    return dialogues.find_one({"event_id": event["event_id"]})


async def create_subscription(subscription_name, stream_name, conn):
    await conn.create_subscription(subscription_name, stream_name)


async def responder_fn(_slack_client):
    _loop = asyncio.get_event_loop()
    async with connect(
            host=EVENT_STORE_URL,
            port=EVENT_STORE_TCP_PORT,
            username=EVENT_STORE_USER,
            password=EVENT_STORE_PASS,
            loop=_loop
    ) as c:
        await c.connect()
        try:
            await create_subscription("slackposter", "aggregate", c)
        except exceptions.SubscriptionCreationFailed as e:
            if e.message.find("'slackposter' already exists."):
                log.info("Slackposter aggregate subscription found.")
            else:
                raise e
        responses_to_send = await c.connect_subscription("slackposter", "aggregate")
        async for event in responses_to_send.events:
            if event.type == "response_created":
                event_obj = json.loads(event.event.data)
                log.debug("responder_fn() responding to: %s" % json.dumps(event_obj))
                dialogue_object = await fetch_dialogue_object(event.event.data)
                try:
                    await slackclient_call(
                        _slack_client,
                        dialogue_object["response"],
                        dialogue_object["event"]["channel"],
                        dialogue_object["event"]["ts"]
                    )
                    await post_to_dialogue_stream(dialogue_object["event_id"])
                    await responses_to_send.ack(event)
                except Exception as e:
                    log.exception(e)
            else:
                await responses_to_send.ack(event)


if __name__ == "__main__":
    slack_client = SlackClient(SLACK_BOT_TOKEN)
    mainloop = asyncio.get_event_loop()
    mainloop.run_until_complete(responder_fn(slack_client))
