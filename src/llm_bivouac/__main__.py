import asyncio
import logging
import os
import signal
import sys

import aiohttp
from aiohttp import web

from .config import Config, load_config
from .manager import Manager
from .ovh import OvhClient
from .server import ApiServer, make_admin_app, make_metrics_app, make_probe
from .services import AccessChecker, Notifier

log = logging.getLogger("llm_bivouac")


async def serve(config: Config) -> None:
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        ovh = OvhClient(config, session)
        manager = Manager(config, ovh, Notifier(config, session), make_probe(session, config))
        api = ApiServer(config, manager, AccessChecker(config, session), session)

        runners = []
        for application in (api.app(), make_admin_app(manager, ovh), make_metrics_app(manager)):
            runner = web.AppRunner(application, access_log=None)
            await runner.setup()
            runners.append(runner)

        await web.TCPSite(runners[0], "0.0.0.0", config.listen_port).start()
        if os.path.exists(config.socket_path):
            os.unlink(config.socket_path)
        await web.UnixSite(runners[1], config.socket_path).start()
        os.chmod(config.socket_path, 0o600)
        await web.TCPSite(runners[2], "0.0.0.0", config.metrics_port).start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)

        controller = asyncio.create_task(manager.run_forever())
        log.info("proxy ready on port %s", config.listen_port)
        await stop.wait()

        # Apps keep running: OVH holds their state and each one can delete itself.
        controller.cancel()
        for runner in runners:
            await runner.cleanup()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(load_config()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
