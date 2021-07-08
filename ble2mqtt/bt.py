import asyncio as aio
import logging
from contextlib import asynccontextmanager

from bleak import BleakError

logger = logging.getLogger(__name__)
ListOfBtConnectionErrors = (
    BleakError,
    aio.TimeoutError,

    # dbus-next exceptions:
    # AttributeError: 'NoneType' object has no attribute 'call'
    AttributeError,
    # https://github.com/hbldh/bleak/issues/409
    EOFError,
)

BLUETOOTH_RESTARTING = aio.Lock()


def hardware_exception_occurred(exception):
    ex_str = str(exception)
    return (
        'org.freedesktop.DBus.Error.ServiceUnknown' in ex_str or
        'org.freedesktop.DBus.Error.NoReply' in ex_str or
        'org.freedesktop.DBus.Error.AccessDenied' in ex_str or
        'org.bluez.Error.Failed: Connection aborted' in ex_str or
        'org.bluez.Error.NotReady' in ex_str or
        'org.bluez.Error.InProgress' in ex_str
    )


async def restart_bluetooth():
    if BLUETOOTH_RESTARTING.locked():
        await aio.sleep(9)
        return
    async with BLUETOOTH_RESTARTING:
        logger.warning('Restarting bluetoothd...')
        proc = await aio.create_subprocess_exec(
            'hciconfig', 'hci0', 'down',
        )
        await proc.wait()
        proc = await aio.create_subprocess_exec(
            '/etc/init.d/bluetoothd', 'restart',
        )
        await proc.wait()
        await aio.sleep(3)
        proc = await aio.create_subprocess_exec(
            'hciconfig', 'hci0', 'up',
        )
        await proc.wait()
        await aio.sleep(5)
        logger.warning('Restarting bluetoothd finished')


@asynccontextmanager
async def handle_ble_exceptions():
    try:
        yield
    except ListOfBtConnectionErrors as e:
        if hardware_exception_occurred(e):
            await restart_bluetooth()
            await aio.sleep(3)
        raise
