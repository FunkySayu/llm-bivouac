import asyncio
import logging
import os
import signal
import sys

import aiohttp
from aiohttp import web

from .config import Config, load_config
from .ledger import Ledger
from .manager import Manager
from .ovh import OvhClient
from .server import ApiServer, make_admin_app, make_metrics_app, make_probe
from .services import AccessChecker, Notifier
from .state import StateStore

log = logging.getLogger("llm_bivouac")
LEDGER_RETRY_SECONDS = 15


async def connect_ledger(ledger: Ledger) -> None:
    """Keep trying in the background: deadlines are enforced even without Postgres."""
    while True:
        try:
            await ledger.connect()
        except Exception as error:
            log.warning("Postgres is unavailable, new starts are refused: %s", error)
            await asyncio.sleep(LEDGER_RETRY_SECONDS)
        else:
            log.info("connected to Postgres")
            return


async def serve(config: Config) -> None:
    store = StateStore(config.state_dir)
    ledger = Ledger(config.database_url, config.budget_pool_eur, config.budget_refuse_at_eur)
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        ovh = OvhClient(config, session)
        manager = Manager(
            config, store, ledger, ovh, Notifier(config, session), make_probe(session, config)
        )
        api = ApiServer(config, manager, AccessChecker(config, session), session)

        runners = []
        for application in (api.app(), make_admin_app(manager, ledger, ovh)):
            runner = web.AppRunner(application, access_log=None)
            await runner.setup()
            runners.append(runner)
        metrics_runner = web.AppRunner(make_metrics_app(manager, ledger), access_log=None)
        await metrics_runner.setup()
        runners.append(metrics_runner)

        await web.TCPSite(runners[0], "0.0.0.0", config.listen_port).start()
        if os.path.exists(config.socket_path):
            os.unlink(config.socket_path)
        await web.UnixSite(runners[1], config.socket_path).start()
        os.chmod(config.socket_path, 0o600)
        await web.TCPSite(metrics_runner, "0.0.0.0", config.metrics_port).start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)

        background = [
            asyncio.create_task(connect_ledger(ledger)),
            asyncio.create_task(manager.run_forever()),
        ]
        log.info("proxy ready on port %s", config.listen_port)
        await stop.wait()

        # Deployments keep running: the state file and in-app self-destruct cover them.
        for task in background:
            task.cancel()
        for runner in runners:
            await runner.cleanup()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(load_config()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
