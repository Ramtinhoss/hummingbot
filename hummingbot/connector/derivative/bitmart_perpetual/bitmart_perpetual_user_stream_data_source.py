import asyncio
from typing import TYPE_CHECKING, List, Optional

import hummingbot.connector.derivative.bitmart_perpetual.bitmart_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.bitmart_perpetual.bitmart_perpetual_auth import BitmartPerpetualAuth
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest, WSResponse
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.bitmart_perpetual.bitmart_perpetual_derivative import (
        BitmartPerpetualDerivative,
    )


class BinancePerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    LISTEN_KEY_KEEP_ALIVE_INTERVAL = 1800  # Recommended to Ping/Update listen key to keep connection alive
    HEARTBEAT_TIME_INTERVAL = 30.0
    _logger: Optional[HummingbotLogger] = None

    def __init__(
            self,
            auth: BitmartPerpetualAuth,
            connector: 'BitmartPerpetualDerivative',
            api_factory: WebAssistantsFactory,
            domain: str = CONSTANTS.DOMAIN,
    ):

        super().__init__()
        self._domain = domain
        self._api_factory = api_factory
        self._auth = auth
        self._ws_assistants: List[WSAssistant] = []
        self._connector = connector
        self._current_listen_key = None
        self._listen_for_user_stream_task = None
        self._last_listen_key_ping_ts = None

        self._manage_listen_key_task = None
        self._listen_key_initialized_event: asyncio.Event = asyncio.Event()

    @property
    def last_recv_time(self) -> float:
        if self._ws_assistant:
            return self._ws_assistant.last_recv_time
        return 0

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    async def _listen_for_user_stream_on_url(self, url: str, output: asyncio.Queue):
        ws: Optional[WSAssistant] = None
        while True:
            try:
                ws = await self._get_connected_websocket_assistant(url)
                self._ws_assistants.append(ws)
                await self._subscribe_to_channels(ws, url)
                await self._process_websocket_messages(websocket_assistant=ws, queue=output)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception(
                    f"Unexpected error while listening to user stream {url}. Retrying after 5 seconds..."
                )
                await self._sleep(5.0)
            finally:
                await self._on_user_stream_interruption(ws)
                ws and self._ws_assistants.remove(ws)

    async def _get_connected_websocket_assistant(self, ws_url: str) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=ws_url, message_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE)
        await self._authenticate(ws)
        return ws

    async def _authenticate(self, ws: WSAssistant):
        """
        Authenticates user to websocket
        """
        login_request: WSJSONRequest = WSJSONRequest(payload=self._auth.get_ws_login_with_args())
        await ws.send(login_request)
        response: WSResponse = await ws.receive()
        message = response.data

        if not message["success"]:
            self.logger().error("Error authenticating the private websocket connection")
            raise IOError("Private websocket connection authentication failed")

    async def _subscribe_to_channels(self, ws: WSAssistant, url: str):
        try:
            channels_to_subscribe: List[str] = [
                CONSTANTS.WS_POSITIONS_CHANNEL,
                CONSTANTS.WS_ORDERS_CHANNEL,
                CONSTANTS.WS_ACCOUNT_CHANNEL
            ]

            tasks = []
            for channel in channels_to_subscribe:
                payload = {
                    "action": "subscribe",
                    "args": [channel]
                }
                task = ws.send(WSJSONRequest(payload))
                tasks.append(task)

            await asyncio.gather(*tasks)

            self.logger().info(
                f"Subscribed to private account and orders channels {url}..."
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(
                f"Unexpected error occurred subscribing to private account and orders channels {url}..."
            )
            raise
