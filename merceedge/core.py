import logging
import threading
import enum
import os
import sys
import copy
import json
import asyncio
import attr
import uuid
import functools
from concurrent.futures import ThreadPoolExecutor
from async_timeout import timeout
from typing import (  # noqa: F401 pylint: disable=unused-import
    Optional, Any, Callable, List, TypeVar, Dict, Coroutine, Set,
    TYPE_CHECKING, Awaitable, Iterator)

from os.path import join
dir_path = os.path.dirname(os.path.realpath(__file__))

import merceedge.util as util
import merceedge.util.dt as dt_util
import merceedge.util.id as id_util
import merceedge.util.yaml as yaml_util
import merceedge.util.module as module_util
from merceedge.util.async_util import (
    Context,
    callback,
    is_callback,
    run_callback_threadsafe,
    run_coroutine_threadsafe,
    fire_coroutine_threadsafe,
    CALLBACK_TYPE,
    T
)
from merceedge.exceptions import MerceEdgeError
from merceedge.const import (
    MATCH_ALL,
    EVENT_TIME_CHANGED,
    EVENT_SERVICE_EXECUTED,
    EVENT_CALL_SERVICE,
    EVENT_STATE_CHANGED
)
from merceedge.service import ServiceRegistry
from merceedge.providers import ServiceProviderFactory
from merceedge.api_server.models import (
    ComponentDBModel, 
    WireDBModel
)

DOMAIN = "merceedge"


_LOGGER = logging.getLogger(__name__)


class MerceEdge(object):
    """Root object of Merce Edge node"""
    def __init__(self, user_config):
        self.user_config = user_config

        self.loop = asyncio.get_event_loop()
        executor_opts = {'max_workers': None}  # type: Dict[str, Any]
        if sys.version_info[:2] >= (3, 6):
            executor_opts['thread_name_prefix'] = 'SyncWorker'
        self.executor = ThreadPoolExecutor(**executor_opts)
        self.loop.set_default_executor(self.executor)
        self._pending_tasks = []  # type: list
        self._track_task = True
        self.exit_code = 0

        self.bus = EventBus(self)
        self.services = ServiceRegistry(self)
        
        self.component_templates = {} # key: component template name
        self.components = {}  # key: component id
        self.wires = {} # key: wire id
        self.wireload_factory = WireLoadFactory(user_config)
        # ServiceProviderFactory.init(user_config['provider_path'])
        # self.states = StateMachine(self.bus)
        # self.config = Config()
        # self._lock = threading.Lock()
        
    def dyload_component(self, component_config):
        """dynamic load new component"""
        # TODO
    
    def start(self):
        """Start.

        Note: This function is only used for testing.
        For regular use, use "await edge.run()".
        """
        # Register the async start
        fire_coroutine_threadsafe(self.async_start(), self.loop)

        # Run forever
        try:
            # Block until stopped
            _LOGGER.info("Starting Home Assistant core loop")
            self.loop.run_forever()
        finally:
            self.loop.close()
        return self.exit_code

    def stop(self):
        # TODO 
        pass

    def load_local_component_templates(self, component_template_path):
        """Read local component templates path, generate component template objects
        """
        template_configs = []
        template_configs += [each for each in os.listdir(component_template_path) if each.endswith('.yaml')]
        for template_config in template_configs:
            com_tmp_yaml = yaml_util.load_yaml(join(component_template_path, template_config))
            # new_com_tmp = Component(com_tmp_yaml)
            self.component_templates[com_tmp_yaml['component']['name']] = com_tmp_yaml
            

    def generate_component_instance(self, component_template_name, id=None):
        """Deepcopy component from component template
        """
        com_tmp_yaml = self.component_templates.get(component_template_name, None)
        if com_tmp_yaml:
            new_com = Component(self, com_tmp_yaml, id)
            self.components[new_com.id] = new_com
            return new_com
        else:
            # TODO logger warn no such  name component compnent
            pass
        return None

    async def connect_interface(self, output_component_id, output_name, input_component_id, input_name, wire_id=None, wireload_name=None):
        """ connenct wire
        """
        wire = Wire(edge=self, 
                    output_sink=self.components[output_component_id].outputs[output_name], 
                    input_slot=self.components[input_component_id].inputs[input_name], 
                    wireload_name=wireload_name,
                    id=wire_id)
        self.wires[wire.id] = wire

        await self.components[output_component_id].outputs[output_name].conn_output_sink()
        return wire

    
    def delete_wire(self, wire_id):
        """Disconnect wire
        """
        try:
            wire = self.wires[wire_id]
            wire.disconnect()
            del self.wires[wire.id]
            return wire
        except KeyError:
            return None

    def restore_entities_from_db(self):
        """Restore components / wires from local db when edge start.
            1. 获取所有的组件信息, 根据组件类型名称创建组件对象， 注意：组件的uuid从记录读取
            2. 获取所有的连线信息，连接相关接口
        """
        # TODO
        # Restruct components
        component_db_list = ComponentDBModel.query.all()
        for component_db_record in component_db_list:
            self.generate_component_instance(component_db_record.template_name, 
                                                            component_db_record.uuid)
        # Restruct wires
        wire_db_list = WireDBModel.query.all()
        for wire_db_record in wire_db_list:
            try:
                output_component_uuid = wire_db_record.output_component_uuid
                input_component_uuid = wire_db_record.input_component_uuid
                output_name = wire_db_record.output_name
                input_name = wire_db_record.input_name
                wire_id = wire_db_record.id
                self.connect_interface(output_component_uuid, output_name,
                                     input_component_uuid, input_name, 
                                     wire_id)
            except KeyError:
                # TODO logger warn
                continue
    
    async def load_formula(self, formula_path):
        formula_yaml = yaml_util.load_yaml(formula_path)
        wires = formula_yaml['wires']
        try:
            for wire in wires:
                # struct components
                output_com = self.generate_component_instance(wire['output_slot']['component'])
                input_com = self.generate_component_instance(wire['input_sink']['component'])
                # struct wire
                output_name = wire['output_slot']['output']['name']
                input_name = wire['input_sink']['input']['name']

                # wireload is optional
                wireload_name = None
                wireload = wire.get('wireload', None)
                if wireload:
                    wireload_name = wireload['name']
                
                await self.connect_interface(output_com.id, output_name,
                                        input_com.id, input_name,
                                        wireload_name=wireload_name)
        except KeyError:
            _LOGGER.error("Load formula error, program exit!")
            sys.exit(-1)

    def add_job(self, target: Callable[..., None], *args: Any) -> None:
        """Add job to the executor pool.

        target: target to call.
        args: parameters for method to call.
        """
        if target is None:
            raise ValueError("Don't call add_job with None")
        self.loop.call_soon_threadsafe(self.async_add_job, target, *args)

    @callback
    def async_add_job(
            self,
            target: Callable[..., Any],
            *args: Any) -> Optional[asyncio.Future]:
        """Add a job from within the event loop.

        This method must be run in the event loop.

        target: target to call.
        args: parameters for method to call.
        """
        task = None

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        if asyncio.iscoroutine(check_target):
            task = self.loop.create_task(target)  # type: ignore
        elif is_callback(check_target):
            self.loop.call_soon(target, *args)
        elif asyncio.iscoroutinefunction(check_target):
            task = self.loop.create_task(target(*args))
        else:
            task = self.loop.run_in_executor(  # type: ignore
                None, target, *args)

        # If a task is scheduled
        if self._track_task and task is not None:
            self._pending_tasks.append(task)

        return task
    
    @callback
    def async_run_job(self, target: Callable[..., None], *args: Any) -> None:
        """Run a job from within the event loop.

        This method must be run in the event loop.

        target: target to call.
        args: parameters for method to call.
        """
        if not asyncio.iscoroutine(target) and is_callback(target):
            target(*args)
        else:
            self.async_add_job(target, *args)
    
    @callback
    def async_create_task(self, target: Coroutine) -> asyncio.tasks.Task:
        """Create a task from within the eventloop.

        This method must be run in the event loop.

        target: target to call.
        """
        task = self.loop.create_task(target)  # type: asyncio.tasks.Task

        if self._track_task:
            self._pending_tasks.append(task)

        return task

    @callback
    def async_add_executor_job(
            self,
            target: Callable[..., T],
            *args: Any) -> Awaitable[T]:
        """Add an executor job from within the event loop."""
        task = self.loop.run_in_executor(
            None, target, *args)

        # If a task is scheduled
        if self._track_task:
            self._pending_tasks.append(task)

        return task

    @callback
    def async_track_tasks(self) -> None:
        """Track tasks so you can wait for all tasks to be done."""
        self._track_task = True

    @callback
    def async_stop_track_tasks(self) -> None:
        """Stop track tasks so you can't wait for all tasks to be done."""
        self._track_task = False

    def block_till_done(self) -> None:
        """Block till all pending work is done."""
        run_coroutine_threadsafe(
            self.async_block_till_done(), loop=self.loop).result()

    async def async_block_till_done(self) -> None:
        """Block till all pending work is done."""
        # To flush out any call_soon_threadsafe
        await asyncio.sleep(0)

        while self._pending_tasks:
            pending = [task for task in self._pending_tasks
                       if not task.done()]
            self._pending_tasks.clear()
            if pending:
                await asyncio.wait(pending)
            else:
                await asyncio.sleep(0)
    
    async def async_run(self) -> int:
        """ MerceEdge main entry point.

        Start and block until stopped.

        This method is a coroutine.
        """
        # _async_stop will set this instead of stopping the loop
        self._stopped = asyncio.Event()

        await self.async_start()
      
        await self._stopped.wait()
        return self.exit_code
    
    async def async_start(self) -> None:
        """Finalize startup from inside the event loop.

        This method is a coroutine.
        """
        _LOGGER.info("Starting Merce Edge")
        # self.state = CoreState.starting

        setattr(self.loop, '_thread_ident', threading.get_ident())
        # self.bus.async_fire(EVENT_HOMEASSISTANT_START)

        try:
            # Only block for EVENT_HOMEASSISTANT_START listener
            self.async_stop_track_tasks()
            with timeout(15):
                await self.async_block_till_done()
        except asyncio.TimeoutError:
            # TODO warning
            pass
            # _LOGGER.warning(
            #     'Something is blocking Home Assistant from wrapping up the '
            #     'start up phase. We\'re going to continue anyway. Please '
            #     'report the following info at http://bit.ly/2ogP58T : %s',
            #     ', '.join(self.config.components))

        # Allow automations to set up the start triggers before changing state
        await asyncio.sleep(0)

        # if self.state != CoreState.starting:
        #     _LOGGER.warning(
        #         'Home Assistant startup has been interrupted. '
        #         'Its state may be inconsistent.')
        #     return

        # self.state = CoreState.running
        # _async_create_timer(self)


class Entity(object):
    """ABC for Merce Edge entity(Component, Interface, etc.)"""
    id = id_util.generte_unique_id()
    attrs = {}

    def load_attrs(self, config):
        # TODO
        raise NotImplementedError 
    
    def get_attrs(self, attr_key):
        try:
            return self.attrs.get(attr_key)
        except KeyError as e:
            _LOGGER.error(str(e))
            return None
    
    def set_attrs(self, _attrs):
        self.attrs.update(_attrs)


class Component(Entity):
    """ABC for Merce Edge components"""
    
    def __init__(self, edge, model_template_config, id=None):
        """
        model_template_config: yaml object
        """
        self.edge = edge
        self.model_template_config = model_template_config
        self.id = id or id_util.generte_unique_id()
        self.inputs = {}
        self.outputs = {}
        # self.components = {}
        
        # init interfaces
        self._init_interfaces()
    
    def _init_interfaces(self):
        """initiate inputs & outputs
        """
        inputs = self.model_template_config['component'].get('inputs', None)
        if inputs:
            for _input in inputs:
                self.inputs[_input['name']] = Input(self.edge, _input['name'], self, _input['protocol']['name'], _input['protocol'])
        
        outputs = self.model_template_config['component'].get('outputs', None)
        if outputs:
            for _ouput in outputs:
                self.outputs[_ouput['name']] = Output(self.edge, _ouput['name'], self, _ouput['protocol']['name'], _ouput['protocol'])   

    def get_start_wires_info(self):
        """ Get wires infomation that start from component
        """
        wires = []
        for output in self.outputs:
            for wire in output.output_wires:
                # TODO 
                pass
        return wires

    def update_state(self, data):
        # TODO update state
        pass
    

class Interface(Entity):
    """Interface ABC 
    1. Read configuration file and load interface using service(eg: mqtt service).
    2. Listen message from EventBus, or call fire event provide by service(eg: mqtt service).
    """
    def __init__(self, edge, name, component, protocol, attrs=None):
        self.edge = edge
        self.name = name
        self.component = component
        self.protocol = protocol
        self.attrs = attrs or {}
    
   
class Output(Interface):
    """Virtual output interface, receive data from real world
    """
    def __init__(self, edge, name, component, protocol, attrs):
        super(Output, self).__init__(edge, name, component, protocol, attrs)
        self.output_wires = {}
        self.data = {}

        # read output configuration
        # print("init output {} {}".format(name, protocol))
        self._init_provider()

    def wires_info(self):
        info = {}
        for wire_id, wire in self.output_wires.items():
            info[wire_id] = wire.__repr__()
        return info
    
    def add_wire(self, wire):
        """Add new wire"""
        self.output_wires[wire.id] = wire
    
    def del_wire(self, wire_id):
        """Remove wire
        """
        self.provider.disconn_output_sink(self)
        del self.output_wires[wire_id]
        print("output wires: {}".format(self.output_wires))
    
    def _init_provider(self):
        try:
            self.provider = ServiceProviderFactory.get_provider(self.protocol)
            _LOGGER.debug("Output {} load provider {}".format(self.name, self.provider))
            # if self.provider:
            #     self.provider.new_instance_setup(self.name, self.attrs, True)
        except KeyError as e:
            # log no such provider key error
            _LOGGER.error("Cannot load {} provider".format(self.protocol))
            raise
    
    async def conn_output_sink(self, output_wire_params={}):
        """ register EventBus listener"""
        await self.provider.conn_output_sink(output=self, 
                                       output_wire_params=output_wire_params,
                                       callback=self._output_sink_callback)
    
    def _output_sink_callback(self, payload):
        """Emit data to all wires
            1. get all wire input_sink
            2. emit data into input sink
        """
        #TODO self.protocol_provider.output_sink()

        for wire_id, wire in self.output_wires.items():
            self.edge.add_job(wire.fire, payload)
    
        


class Input(Interface):
    """Input"""
    def __init__(self, edge, name, component, protocol, attrs):
        super(Input, self).__init__(edge, name, component, protocol, attrs)
        # self.component = component
        self.input_wires = {}
        self._conn_input_slot()
    
    def wires_info(self):
        info = {}
        for wire_id, wire in self.input_wires.items():
            info[wire_id] = wire.__repr__()
        return json.dumps(info)

    def add_wire(self, wire):
        """Add new wire"""
        self.input_wires[wire.id] = wire
    
    def del_wire(self, wire_id):
        """Remove wire
        """
        del self.input_wires[wire_id]
        
    def _conn_input_slot(self):
        try:
            self.provider = ServiceProviderFactory.get_provider(self.protocol)
        except KeyError as e:
            # TODO log no such provider key error
            raise
    
    async def emit_data_to_input(self, payload):
        # Emit data to EventBus and invoke configuration service send data function.
        # TODO payload根据wire类型进行转换
        print("emit_data_to_input:")
        print(payload)
        await self.provider.emit_input_slot(self, payload.data)


class State(object):
    """Component State"""
    # raise NotImplementedError
    # TODO
    pass


class Wire(Entity):
    """Wire """
    def __init__(self, edge, output_sink, input_slot, wireload_name=None, id=None):
        self.edge = edge
        self.id = id or id_util.generte_unique_id()
        self.input = output_sink
        self.output = input_slot
        self.input.add_wire(self)
        self.output.add_wire(self)
        
        self.input_params = dict()
        self.output_params = dict()

        # eg: condition if ... elif ... else ... 
        # filter/AI/custom module, etc...
        self.wire_load = None
        print("wireloadname {}".format(wireload_name))
        if wireload_name:
            self._create_wireload_object(wireload_name)
        # self.edge.bus.listen("wire_ouput_{}".format(self.id), self.wire_output_handler)
        
    def __repr__(self):
        wire_info = {}
        wire_info["input"] = {"component_id": self.input.component.id, 
                            "name": self.input.name}
        wire_info["output"] = {"component_id": self.output.component.id,
                            "name": self.output.name}
        return wire_info    
    
    def _create_wireload_object(self, wireload_name):
        wireload_class = self.edge.wireload_factory.get_class(wireload_name)
        if wireload_class:
            self.wire_load = wireload_class()

    def set_input_params(self, parameters):
        self.input_params = parameters

    def set_output_params(self, parameters):
        self.output_params = parameters
    
    # @property
    # def input(self):
    #     return self.input
    
    # @property
    # def output(self):
    #     return self.output

    def disconnect(self):
        self.input.del_wire(self.id)
        self.output.del_wire(self.id)
            
    async def fire(self, payload):
        """Fire payload data from input to output"""
        #  send event to eventbus with wire_output_{wireid} event
        # self.edge.bus.fire("wire_ouput_{}".format(self.id), payload)
        data = payload
        if self.wire_load:
            data = self.wire_load.process(data)
            if data is not None:
                await self.output.emit_data_to_input(data)

        elif type(data).__module__ != 'numpy' and data is not None:
            await self.output.emit_data_to_input(data)
    
    # def wire_output_handler(self, payload):
    #     data = payload.data
    #     if self.wire_load:
    #         data = self.wire_load.process(data)
    #     self.output.emit_data_to_input(data)


class WireLoadFactory:
    def __init__(self, config):
        """
        config: user configuration
        """
        self._classes = {}
        path = os.path.join(dir_path, config['wireload']['path'])
        self._load(path)

    def _load(self, path):
        """Walk throuth path and load WireLoad subclass
        """
        self._classes = module_util.load_modules(path, WireLoad)
        _LOGGER.debug("Load wireloads modules: {}".format(self._classes))
        
    def get_class(self, wireload_name):
        return self._classes.get(wireload_name, None)


class WireLoad(Entity):
    """Wire load abstract class. Mounted on wire, processing data through wire.
        Filter, Analiysis, Process, etc.
    """
    name = ''
    def __init__(self, init_params={}):
        self.init_params = init_params
        self.ouput_data = None
    
    def process(self, input_data):
        raise NotImplementedError

    @property
    def output(self):
        return self.ouput_data
    
 


class Event(object):
    # pylint: disable=too-few-public-methods
    """Represents an event within the Bus."""

    __slots__ = ['event_type', 'data', 'time_fired', 'context']

    def __init__(self, event_type: str, data: Optional[Dict] = None,
                 time_fired: Optional[int] = None,
                 context: Optional[Context] = None) -> None:
        """Initialize a new event."""
        self.event_type = event_type
        self.data = data or {}
        self.time_fired = time_fired or dt_util.utcnow()
        self.context = context or Context()

    def as_dict(self) -> Dict:
        """Create a dict representation of this Event."""
        return {
            'event_type': self.event_type,
            'data': dict(self.data),
            'time_fired': self.time_fired,
            'context': self.context.as_dict()
        }

    def __repr__(self) -> str:
        # pylint: disable=maybe-no-member
        """Return the representation."""
        # pylint: disable=maybe-no-member
        if self.data:
            return "<Event {}: {}>".format(
                self.event_type,
                util.repr_helper(self.data))

        return "<Event {}>".format(self.event_type)

    def __eq__(self, other: Any) -> bool:
        """Return the comparison."""
        return (self.__class__ == other.__class__ and  # type: ignore
                self.event_type == other.event_type and
                self.data == other.data and
                self.time_fired == other.time_fired and
                self.context == other.context)


class EventBus(object):
    """Allows firing of and listening for events.

       NOTE: This part of code references home-assistant and chage a little.
    """

    def __init__(self, edge: MerceEdge) -> None:
        """Initialize a new event bus."""
        self._listeners = {} # type: Dict[str, List[Callable]]
        self.edge = edge
    
    @callback
    def async_listeners(self) -> Dict[str, int]:
        """Dict with events and the number of listeners."""
        return {key: len(self._listeners[key])
                for key in self._listeners}

    @property
    def listeners(self) -> Dict[str, int]:
         """Dict with events and the number of listeners.
         """
         return run_callback_threadsafe(  # type: ignore
            self.edge.loop, self.async_listeners
        ).result()

    def fire(self, event_type: str, event_data: Optional[Dict] = None,
             context: Optional[Context] = None) -> None:
        """Fire an event."""
        print(self.edge.loop)
        self.edge.loop.call_soon_threadsafe(
            self.async_fire, event_type, event_data, context)
    
    @callback
    def async_fire(self, event_type: str, event_data: Optional[Dict] = None,
             context: Optional[Context] = None) -> None:
        """Fire an event.
        This method must be run in the event loop
        """
        print("asnyc_fire")
        listeners = self._listeners.get(event_type, [])

        # EVENT_HOMEASSISTANT_CLOSE should go only to his listeners
        match_all_listeners = self._listeners.get(MATCH_ALL)
        if (match_all_listeners is not None):
            listeners = match_all_listeners + listeners

        event = Event(event_type, event_data, None, context)

        if event_type != EVENT_TIME_CHANGED:
            _LOGGER.debug("Bus:Handling %s", event)

        if not listeners:
            return

        for func in listeners:
            self.edge.async_add_job(func, event)
    
    def listen(
            self, event_type: str, listener: Callable) -> CALLBACK_TYPE:
        """Listen for all events or events of a specific type.

        To listen to all events specify the constant ``MATCH_ALL``
        as event_type.
        """
        async_remove_listener = run_callback_threadsafe(
            self.edge.loop, self.async_listen, event_type, listener).result()

        def remove_listener() -> None:
            """Remove the listener."""
            run_callback_threadsafe(
                self.edge.loop, async_remove_listener).result()

        return remove_listener
    
    @callback
    def async_listen(self, event_type: str, listener: Callable) -> CALLBACK_TYPE:
        """Listen for all events or events of a specific type.

        To listen to all events specify the constant ``MATCH_ALL``
        as event_type.

        This method must be run in the event loop.
        """
        if event_type in self._listeners:
            self._listeners[event_type].append(listener)
        else:
            self._listeners[event_type] = [listener]

        def remove_listener() -> None:
            """Remove the listener."""
            self._async_remove_listener(event_type, listener)

        return remove_listener

    def listen_once(
            self, event_type: str, listener: Callable) -> CALLBACK_TYPE:
        """Listen once for event of a specific type.

        To listen to all events specify the constant ``MATCH_ALL``
        as event_type.

        Returns function to unsubscribe the listener.
        """
        async_remove_listener = run_callback_threadsafe(
            self.edge.loop, self.asnyc_listen_once, event_type, listener,
        ).result()

        def remove_listener() -> None:
            """Remove the listener."""
            run_callback_threadsafe(
                self.edge.loop, async_remove_listener).result()

        return remove_listener

    @callback
    def asnyc_listen_once(
            self, event_type: str, listener: Callable) -> CALLBACK_TYPE:
        """Listen once for event of a specific type.

        To listen to all events specify the constant ``MATCH_ALL``
        as event_type.

        Returns registered listener that can be used with remove_listener.

        This method must be run in the event loop.
        """
        @callback
        def onetime_listener(event: Event) -> None:
            """Remove listener from event bus and then fire listener."""
            if hasattr(onetime_listener, 'run'):
                return
            # Set variable so that we will never run twice.
            # Because the event bus loop might have async_fire queued multiple
            # times, its possible this listener may already be lined up
            # multiple times as well.
            # This will make sure the second time it does nothing.
            setattr(onetime_listener, 'run', True)
            self._async_remove_listener(event_type, onetime_listener)
            self.edge.async_run_job(listener, event)

        return self.async_listen(event_type, onetime_listener)

    @callback
    def _async_remove_listener(
            self, event_type: str, listener: Callable) -> None:
        """Remove a listener of a specific event_type.

        This method must be run in the event loop.
        """
        try:
            self._listeners[event_type].remove(listener)

            # delete event_type list if empty
            if not self._listeners[event_type]:
                self._listeners.pop(event_type)
        except (KeyError, ValueError):
            # KeyError is key event_type listener did not exist
            # ValueError if listener did not exist within event_type
            _LOGGER.warning("Unable to remove unknown listener %s", listener)






