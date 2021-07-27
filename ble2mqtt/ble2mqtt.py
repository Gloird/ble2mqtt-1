import asyncio as aio
import json
import logging
import typing as ty
from functools import partial
from uuid import getnode

import aio_mqtt
from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from .bt import (ListOfBtConnectionErrors, handle_ble_exceptions,
                 restart_bluetooth)
from .connections import ActiveConnectionManager
from .devices.base import (BINARY_SENSOR_DOMAIN, COVER_DOMAIN,
                           DEVICE_TRACKER_DOMAIN, LIGHT_DOMAIN, SELECT_DOMAIN,
                           SENSOR_DOMAIN, SWITCH_DOMAIN, ConnectionMode,
                           ConnectionTimeoutError, Device)
from .helpers import (done_callback, handle_returned_tasks,
                      run_tasks_and_cancel_on_first_return)

_LOGGER = logging.getLogger(__name__)

CONFIG_MQTT_NAMESPACE = 'homeassistant'
BRIDGE_STATE_TOPIC = 'state'
BLUETOOTH_ERROR_RECONNECTION_TIMEOUT = 60


ListOfMQTTConnectionErrors = (
        aio_mqtt.ConnectionLostError,
        aio_mqtt.ConnectionClosedError,
        aio_mqtt.ServerDiedError,
        BrokenPipeError,
)


class DeviceManager:
    def __init__(self, device, *, mqtt_client, base_topic, config_prefix,
                 global_availability_topic):
        self.device: Device = device
        self._mqtt_client = mqtt_client
        self._base_topic = base_topic
        self._config_prefix = config_prefix
        self._global_availability_topic = global_availability_topic
        self.was_initial_connection = False
        self.manage_task = None

    async def close(self):
        if self.manage_task and not self.manage_task.done():
            self.manage_task.cancel()
            try:
                await self.manage_task
            except aio.CancelledError:
                pass
        self.manage_task = None
        try:
            await self.device.disconnect()
        except aio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(f'Problem on closing device {self.device}')

    def run_task(self) -> aio.Task:
        assert not self.manage_task, \
            f'{self.device} Previous task was not finished! {self.manage_task}'
        self.manage_task = aio.create_task(self.manage_device())
        return self.manage_task

    async def publish_topic_callback(self, topic, value, nowait=False):
        _LOGGER.debug(f'call publish callback topic={topic} value={value}')
        if not self._mqtt_client.is_connected():
            _LOGGER.warning(f'{self.device} mqtt is disconnected')
            return
        await self._mqtt_client.publish(
            aio_mqtt.PublishableMessage(
                topic_name='/'.join((self._base_topic, topic)),
                payload=value,
                qos=aio_mqtt.QOSLevel.QOS_1,
            ),
            nowait=nowait,
        )

    def _get_topic(self, dev_id, subtopic, *args):
        return '/'.join(
            filter(None, (self._base_topic, dev_id, subtopic, *args)),
        )

    @property
    def _config_device_topic(self):
        """Add a prefix to avoid interfering with other ble software"""

        return f'{self._config_prefix}{self.device.dev_id}'

    async def send_device_config(self):
        device = self.device
        device_info = {
            'identifiers': [
                device.unique_id,
            ],
            'name': device.unique_name,
            'model': device.model,
        }
        if device.manufacturer:
            device_info['manufacturer'] = device.manufacturer
        if device.version:
            device_info['sw_version'] = device.version

        def get_generic_vals(entity: dict):
            name = entity.pop('name')
            result = {
                'name': f'{name}_{device.friendly_id}',
                'unique_id': f'{name}_{device.dev_id}',
                'device': device_info,
                'availability_mode': 'all',
                'availability': [
                    {'topic': self._global_availability_topic},
                    {'topic': '/'.join(
                        (self._base_topic, self.device.availability_topic),
                    )},
                ],
            }
            icon = entity.pop('icon', None)
            if icon:
                result['icon'] = f'mdi:{icon}'
            entity.pop('topic', None)
            entity.pop('json', None)
            entity.pop('main_value', None)
            result.update(entity)
            return result

        messages_to_send = []
        sensor_entities = device.entities.get(SENSOR_DOMAIN, [])
        sensor_entities.append(
            {
                'name': 'linkquality',
                'unit_of_measurement': 'lqi',
                'icon': 'signal',
                **(
                    {'topic': device.LINKQUALITY_TOPIC}
                    if device.LINKQUALITY_TOPIC else {}
                ),
            },
        )
        entities = {
            **device.entities,
            SENSOR_DOMAIN: sensor_entities,
        }
        for cls, entities in entities.items():
            if cls in (
                BINARY_SENSOR_DOMAIN,
                SENSOR_DOMAIN,
                DEVICE_TRACKER_DOMAIN,
            ):
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(
                        device.unique_id,
                        entity.get('topic', device.STATE_TOPIC),
                    )
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        self._config_device_topic,
                        entity_name,
                        'config',
                    ))
                    if entity.get('json') and entity.get('main_value'):
                        state_topic_part = {
                            'json_attributes_topic': state_topic,
                            'state_topic': state_topic,
                            'value_template':
                                f'{{{{ value_json.{entity["main_value"]} }}}}',
                        }
                    else:
                        state_topic_part = {
                            'state_topic': state_topic,
                            'value_template':
                                f'{{{{ value_json.{entity_name} }}}}',
                        }

                    if cls == DEVICE_TRACKER_DOMAIN:
                        state_topic_part['source_type'] = 'bluetooth_le'

                    payload = json.dumps({
                        **get_generic_vals(entity),
                        **state_topic_part,
                    })
                    _LOGGER.debug(
                        f'Publish config topic={config_topic}: {payload}',
                    )
                    messages_to_send.append(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
            if cls == SWITCH_DOMAIN:
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(
                        device.unique_id,
                        entity.get('topic', device.STATE_TOPIC),
                    )
                    command_topic = '/'.join((state_topic, device.SET_POSTFIX))
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        self._config_device_topic,
                        entity_name,
                        'config',
                    ))
                    payload = json.dumps({
                        **get_generic_vals(entity),
                        'state_topic': state_topic,
                        'command_topic': command_topic,
                    })
                    _LOGGER.debug(
                        f'Publish config topic={config_topic}: {payload}',
                    )
                    messages_to_send.append(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
                    # TODO: send real state on receiving status from a device
                    _LOGGER.debug(f'Publish initial state topic={state_topic}')
                    await self._mqtt_client.publish(
                        aio_mqtt.PublishableMessage(
                            topic_name=state_topic,
                            payload='OFF',
                            qos=aio_mqtt.QOSLevel.QOS_1,
                        ),
                    )
            if cls == LIGHT_DOMAIN:
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(
                        device.unique_id,
                        entity.get('topic', device.STATE_TOPIC),
                    )
                    set_topic = '/'.join((state_topic, device.SET_POSTFIX))
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        self._config_device_topic,
                        entity_name,
                        'config',
                    ))
                    payload = json.dumps({
                        **get_generic_vals(entity),
                        'schema': 'json',
                        'color_mode': bool(entity.get('color_mode', True)),
                        'supported_color_modes': entity.get(
                            'color_mode',
                            ['rgb'],
                        ),
                        'brightness': entity.get('brightness', True),
                        'state_topic': state_topic,
                        'command_topic': set_topic,
                    })
                    _LOGGER.debug(
                        f'Publish config topic={config_topic}: {payload}',
                    )
                    messages_to_send.append(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
            if cls == COVER_DOMAIN:
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(
                        device.unique_id,
                        entity.get('topic', device.STATE_TOPIC),
                    )
                    set_topic = '/'.join((state_topic, device.SET_POSTFIX))
                    set_position_topic = '/'.join(
                        (state_topic, device.SET_POSITION_POSTFIX),
                    )
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        self._config_device_topic,
                        entity_name,
                        'config',
                    ))
                    config_params = {
                        **get_generic_vals(entity),
                        'state_topic': state_topic,
                        'position_topic': state_topic,
                        'json_attributes_topic': state_topic,
                        'value_template': '{{ value_json.state }}',
                        'position_template': '{{ value_json.position }}',
                        'command_topic': set_topic,
                        'set_position_topic': set_position_topic,
                    }
                    payload = json.dumps(config_params)
                    _LOGGER.debug(
                        f'Publish config topic={config_topic}: {payload}',
                    )
                    messages_to_send.append(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
            if cls == SELECT_DOMAIN:
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(
                        device.unique_id,
                        entity.get('topic', device.STATE_TOPIC),
                    )
                    set_topic = '/'.join((state_topic, device.SET_POSTFIX))
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        self._config_device_topic,
                        entity_name,
                        'config',
                    ))
                    config_params = {
                        **get_generic_vals(entity),
                        'state_topic': state_topic,
                        'command_topic': set_topic,
                    }
                    payload = json.dumps(config_params)
                    _LOGGER.debug(
                        f'Publish config topic={config_topic}: {payload}',
                    )
                    messages_to_send.append(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
        await aio.gather(*[
            self._mqtt_client.publish(message)
            for message in messages_to_send
        ])
        device.config_sent = True

    def get_coros(self):
        coros = [
            self.device.handle(
                self.publish_topic_callback,
                send_config=self.send_device_config,
            ),
        ]
        will_handle_messages = bool(self.device.subscribed_topics)
        if will_handle_messages:
            coros.append(
                self.device.handle_messages(self.publish_topic_callback),
            )
        return coros
    async def send_availability(self, value: bool):
        return await self.device.send_availability(
            self.publish_topic_callback,
            value,
        )

    async def _sleep_until_next_connection(self):
        device = self.device
        _LOGGER.debug(
            f'Sleep for {device.RECONNECTION_SLEEP_INTERVAL} secs to '
            f'reconnect to device={device}',
        )
        if device._connection_mode == ConnectionMode.ACTIVE_KEEP_CONNECTION:
            try:
                await aio.wait_for(
                    device._advertisement_seen.wait(),
                    timeout=device.RECONNECTION_SLEEP_INTERVAL,
                )
            except aio.TimeoutError:
                pass
        else:
            await aio.sleep(self.device.RECONNECTION_SLEEP_INTERVAL)

    async def publish_topic_with_availability(self, topic, value):
        # call sequentially to allow HA receive a new value
        await self.publish_topic_callback(topic, value)
        await self.send_availability(True)

    async def on_connect(self):
        # call on_first_connection if it is the first connection
        # (e.g. to fetch device info) and on_each_connection otherwise
        if not self.was_initial_connection:
            if self.device.subscribed_topics:
                await self._mqtt_client.subscribe(*[
                    (
                        '/'.join((self._base_topic, topic)),
                        aio_mqtt.QOSLevel.QOS_1,
                    )
                    for topic in self.device.subscribed_topics
                ])
            _LOGGER.debug(f'[{self.device}] mqtt subscribed')
            self.was_initial_connection = True
            await self.device.on_first_connection()
        else:
            await self.device.on_each_connection()
        self.device.initialized_event.set()

    async def manage_device(self):
        await ActiveConnectionManager(
            self.device,
            self._mqtt_client,
            on_connect=self.on_connect,
        ).run(self.get_coros)


class Ble2Mqtt:
    TOPIC_ROOT = 'ble2mqtt'
    BRIDGE_TOPIC = 'bridge'

    def __init__(
            self,
            host: str,
            port: int = None,
            user: ty.Optional[str] = None,
            password: ty.Optional[str] = None,
            reconnection_interval: int = 10,
            loop: ty.Optional[aio.AbstractEventLoop] = None,
            *,
            base_topic,
            mqtt_config_prefix,
    ) -> None:
        global BLUETOOTH_RESTARTING

        self._mqtt_host = host
        self._mqtt_port = port
        self._mqtt_user = user
        self._mqtt_password = password
        self._base_topic = base_topic
        self._mqtt_config_prefix = mqtt_config_prefix

        self._reconnection_interval = reconnection_interval
        self._loop = loop or aio.get_event_loop()
        BLUETOOTH_RESTARTING = aio.Lock(loop=self._loop)

        self._mqtt_client = aio_mqtt.Client(
            client_id_prefix=f'{base_topic}_',
            loop=self._loop,
        )

        self._device_managers: ty.Dict[Device, DeviceManager] = {}

        self.availability_topic = '/'.join((
            self._base_topic,
            self.BRIDGE_TOPIC,
            BRIDGE_STATE_TOPIC,
        ))

        self.device_registry: ty.List[Device] = []

    async def start(self):
        result = await run_tasks_and_cancel_on_first_return(
            self._loop.create_task(self._connect_mqtt_forever()),
            self._loop.create_task(self._handle_messages()),
        )
        for t in result:
            await t

    async def close(self) -> None:
        for device, manager in self._device_managers.items():
            await manager.close()

        if self._mqtt_client.is_connected:
            try:
                await self._mqtt_client.disconnect()
            except aio.CancelledError:
                raise
            except Exception as e:
                _LOGGER.warning(f'Error on MQTT  disconnecting: {repr(e)}')

    def register(self, device_class: ty.Type[Device], *args, **kwargs):
        device = device_class(*args, **kwargs)
        if not device:
            return
        if not device.is_passive and not device.SUPPORT_ACTIVE:
            raise NotImplementedError(
                f'Device {device.dev_id} doesn\'t support active mode',
            )
        assert device.is_passive or device.ACTIVE_CONNECTION_MODE in (
            ConnectionMode.ACTIVE_POLL_WITH_DISCONNECT,
            ConnectionMode.ACTIVE_KEEP_CONNECTION,
            ConnectionMode.ON_DEMAND_CONNECTION,
        )
        self.device_registry.append(device)

    @property
    def subscribed_topics(self):
        return [
            '/'.join((self._base_topic, topic))
            for device in self.device_registry
            for topic in device.subscribed_topics
        ]

    async def _handle_messages(self) -> None:
        async for message in self._mqtt_client.delivered_messages(
            f'{self._base_topic}/#',
        ):
            _LOGGER.debug(message)
            while True:
                if message.topic_name not in self.subscribed_topics:
                    await aio.sleep(0)
                    continue

                prefix = f'{self._base_topic}/'
                if message.topic_name.startswith(prefix):
                    topic_wo_prefix = message.topic_name[len(prefix):]
                else:
                    topic_wo_prefix = prefix
                for _device in self.device_registry:
                    if topic_wo_prefix in _device.subscribed_topics:
                        device = _device
                        break
                else:
                    raise NotImplementedError('Unknown topic')
                await aio.sleep(0)
                # # TODO: rewrite!
                # if not device.client.is_connected and \
                #         not getattr(device, 'on_demand_connection', False):
                #     logger.warning(
                #         f'Received topic {topic_wo_prefix} '
                #         f'with {message.payload} '
                #         f'but {device.client} is offline',
                #     )
                #     await aio.sleep(5)
                #     continue

                try:
                    value = json.loads(message.payload)
                except ValueError:
                    value = message.payload.decode()

                await device.add_incoming_message(topic_wo_prefix, value)
                break

            await aio.sleep(1)

    async def stop_device_manage_tasks(self):
        for manager in self._device_managers.values():
            try:
                await manager.close()
            except aio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    f'Problem on closing dev manager {manager.device}')

    def device_detection_callback(self, device: BLEDevice, advertisement_data):
        for reg_device in self.device_registry:
            if reg_device.mac.lower() == device.address.lower():
                if device.rssi:
                    # update rssi for all devices if available
                    reg_device.rssi = device.rssi
                if reg_device.is_passive:
                    if device.name:
                        reg_device._model = device.name
                    reg_device.handle_advert(device, advertisement_data)
                else:
                    _LOGGER.debug(
                        f'active device seen: {reg_device} '
                        f'{advertisement_data}',
                    )
                    reg_device.set_advertisement_seen()

    async def scan_devices_task(self):
        empty_scans = 0
        while True:
            # 10 empty scans in a row means that bluetooth restart is required
            if empty_scans >= 10:
                empty_scans = 0
                await restart_bluetooth()

            try:
                async with handle_ble_exceptions():
                    scanner = BleakScanner()
                    scanner.register_detection_callback(
                        self.device_detection_callback,
                    )
                    try:
                        await aio.wait_for(scanner.start(), 10)
                    except aio.TimeoutError:
                        _LOGGER.error('Scanner start failed with timeout')
                    await aio.sleep(3)
                    devices = scanner.discovered_devices
                    await scanner.stop()
                    if not devices:
                        empty_scans += 1
                    else:
                        empty_scans = 0
                    _LOGGER.debug(f'found {len(devices)} devices: {devices}')
            except KeyboardInterrupt:
                raise
            except aio.IncompleteReadError:
                raise
            except ListOfBtConnectionErrors as e:
                _LOGGER.exception(e)
                empty_scans += 1
            await aio.sleep(1)

    async def _run_device_tasks(self, mqtt_connection_fut: aio.Future) -> None:
        has_passive_devices = False
        for dev in self.device_registry:
            self._device_managers[dev] = \
                DeviceManager(
                    dev,
                    mqtt_client=self._mqtt_client,
                    base_topic=self._base_topic,
                    config_prefix=self._mqtt_config_prefix,
                    global_availability_topic=self.availability_topic,
                )
            if dev.is_passive:
                has_passive_devices = True
        _LOGGER.debug("Wait for network interruptions...")

        device_tasks = [
            manager.run_task()
            for manager in self._device_managers.values()
        ]
        if has_passive_devices:
            scan_task = self._loop.create_task(self.scan_devices_task())
            scan_task.add_done_callback(
                partial(done_callback, '{} stopped unexpectedly'),
            )
            device_tasks.append(scan_task)

        futs = [
            mqtt_connection_fut,
            *device_tasks,
        ]

        finished = await run_tasks_and_cancel_on_first_return(
            *futs,
            ignore_futures=[mqtt_connection_fut],
        )

        finished_managers = []
        for d, m in self._device_managers.items():
            if m.manage_task not in finished:
                await m.close()
            else:
                finished_managers.append(m)

        for m in finished_managers:
            await m.close()

        # when mqtt server disconnects, multiple tasks can raise
        # exceptions. We must fetch all of them
        finished = [t for t in futs if t.done() and not t.cancelled()]
        await handle_returned_tasks(*finished)

    async def _connect_mqtt_forever(self) -> None:
        dev_id = hex(getnode())
        while True:
            try:
                mqtt_connection = await self._mqtt_client.connect(
                    host=self._mqtt_host,
                    port=self._mqtt_port,
                    username=self._mqtt_user,
                    password=self._mqtt_password,
                    client_id=f'ble2mqtt_{dev_id}',
                    will_message=aio_mqtt.PublishableMessage(
                        topic_name=self.availability_topic,
                        payload='offline',
                        qos=aio_mqtt.QOSLevel.QOS_1,
                        retain=True,
                    ),
                )
                _LOGGER.info(f'Connected to {self._mqtt_host}')
                await self._mqtt_client.publish(
                    aio_mqtt.PublishableMessage(
                        topic_name=self.availability_topic,
                        payload='online',
                        qos=aio_mqtt.QOSLevel.QOS_1,
                        retain=True,
                    ),
                )
                await self._run_device_tasks(mqtt_connection.disconnect_reason)
            except (aio.CancelledError, KeyboardInterrupt):
                await self._mqtt_client.publish(
                    aio_mqtt.PublishableMessage(
                        topic_name=self.availability_topic,
                        payload='offline',
                        qos=aio_mqtt.QOSLevel.QOS_0,
                        retain=True,
                    ),
                )
                raise
            except Exception:
                _LOGGER.exception(
                    "Connection lost. Will retry in %d seconds.",
                    self._reconnection_interval,
                )
                try:
                    await self.stop_device_manage_tasks()
                except aio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception('Exception in _connect_forever()')
                try:
                    await self._mqtt_client.disconnect()
                except aio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.error('Disconnect from MQTT broker error')
                await aio.sleep(self._reconnection_interval)
