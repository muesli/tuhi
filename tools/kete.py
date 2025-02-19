#!/usr/bin/env python3
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#

from gi.repository import GObject, Gio, GLib
import sys
import argparse
import binascii
import cmd
import errno
import os
import json
import logging
import re
import readline
import struct
import threading
import time
import svgwrite
import xdg.BaseDirectory
import configparser


CONFIG_PATH = os.path.join(xdg.BaseDirectory.xdg_data_home, 'tuhi-kete')


class ColorFormatter(logging.Formatter):
    BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, LIGHT_GRAY = range(30, 38)
    DARK_GRAY, LIGHT_RED, LIGHT_GREEN, LIGHT_YELLOW, LIGHT_BLUE, LIGHT_MAGENTA, LIGHT_CYAN, WHITE = range(90, 98)
    COLORS = {
        'WARNING': LIGHT_RED,
        'INFO': LIGHT_GREEN,
        'DEBUG': LIGHT_GRAY,
        'CRITICAL': YELLOW,
        'ERROR': RED,
    }
    RESET_SEQ = '\033[0m'
    COLOR_SEQ = '\033[%dm'
    BOLD_SEQ = '\033[1m'

    def __init__(self, *args, **kwargs):
        logging.Formatter.__init__(self, *args, **kwargs)

    def format(self, record):
        levelname = record.levelname
        color = self.COLOR_SEQ % (self.COLORS[levelname])
        message = logging.Formatter.format(self, record)
        message = message.replace('$RESET', self.RESET_SEQ)\
                         .replace('$BOLD', self.BOLD_SEQ)\
                         .replace('$COLOR', color)
        for k, v in self.COLORS.items():
            message = message.replace('$' + k, self.COLOR_SEQ % (v + 30))
        return message + self.RESET_SEQ


log_format = '$COLOR%(levelname)s: %(message)s'
logger_handler = logging.StreamHandler()
logger_handler.setFormatter(ColorFormatter(log_format))
logger = logging.getLogger('tuhi-kete')
logger.addHandler(logger_handler)
logger.setLevel(logging.INFO)

TUHI_DBUS_NAME = 'org.freedesktop.tuhi1'
ORG_FREEDESKTOP_TUHI1_MANAGER = 'org.freedesktop.tuhi1.Manager'
ORG_FREEDESKTOP_TUHI1_DEVICE = 'org.freedesktop.tuhi1.Device'
ROOT_PATH = '/org/freedesktop/tuhi1'

ORG_BLUEZ_DEVICE1 = 'org.bluez.Device1'

# remove ':' from the completer delimiters of readline so we can match on
# device addresses
completer_delims = readline.get_completer_delims()
completer_delims = completer_delims.replace(':', '')
readline.set_completer_delims(completer_delims)


def b2hex(bs):
    '''Convert bytes() to a two-letter hex string in the form "1a 2b c3"'''
    hx = binascii.hexlify(bs).decode('ascii')
    return ' '.join([''.join(s) for s in zip(hx[::2], hx[1::2])])


class DBusError(Exception):
    def __init__(self, message):
        self.message = message


class _DBusObject(GObject.Object):
    _connection = None

    def __init__(self, name, interface, objpath):
        GObject.GObject.__init__(self)

        if _DBusObject._connection is None:
            self._connect_to_session()

        self.interface = interface
        self.objpath = objpath

        try:
            self.proxy = Gio.DBusProxy.new_sync(self._connection,
                                                Gio.DBusProxyFlags.NONE, None,
                                                name, objpath, interface, None)
        except GLib.Error as e:
            if (e.domain == 'g-io-error-quark' and
                    e.code == Gio.IOErrorEnum.DBUS_ERROR):
                raise DBusError(e.message)
            else:
                raise e

        if self.proxy.get_name_owner() is None:
            raise DBusError(f'No-one is handling {name}, is the daemon running?')

        self.proxy.connect('g-properties-changed', self._on_properties_changed)
        self.proxy.connect('g-signal', self._on_signal_received)

    def _connect_to_session(self):
        try:
            _DBusObject._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            if (e.domain == 'g-io-error-quark' and
                    e.code == Gio.IOErrorEnum.DBUS_ERROR):
                raise DBusError(e.message)
            else:
                raise e

    def _on_properties_changed(self, proxy, changed_props, invalidated_props):
        # Implement this in derived classes to respond to property changes
        pass

    def _on_signal_received(self, proxy, sender, signal, parameters):
        # Implement this in derived classes to respond to signals
        pass

    def property(self, name):
        p = self.proxy.get_cached_property(name)
        if p is not None:
            return p.unpack()
        return p

    def terminate(self):
        del(self.proxy)


class _DBusSystemObject(_DBusObject):
    '''
    Same as the _DBusObject, but connects to the system bus instead
    '''
    def __init__(self, name, interface, objpath):
        self._connect_to_system()
        super().__init__(name, interface, objpath)

    def _connect_to_system(self):
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            if (e.domain == 'g-io-error-quark' and
                    e.code == Gio.IOErrorEnum.DBUS_ERROR):
                raise DBusError(e.message)
            else:
                raise e


class BlueZDevice(_DBusSystemObject):
    def __init__(self, objpath):
        super().__init__('org.bluez', ORG_BLUEZ_DEVICE1, objpath)
        self.proxy.connect('g-properties-changed', self._on_properties_changed)

    @GObject.Property
    def connected(self):
        return self.proxy.get_cached_property('Connected').unpack()

    def _on_properties_changed(self, obj, properties, invalidated_properties):
        properties = properties.unpack()

        if 'Connected' in properties:
            self.notify('connected')


class TuhiKeteDevice(_DBusObject):
    def __init__(self, manager, objpath):
        _DBusObject.__init__(self, TUHI_DBUS_NAME,
                             ORG_FREEDESKTOP_TUHI1_DEVICE,
                             objpath)
        self.manager = manager
        self.is_registering = False
        self.live = False
        self._bluez_device = BlueZDevice(self.property('BlueZDevice'))
        self._bluez_device.connect('notify::connected', self._on_connected)

    @classmethod
    def is_device_address(cls, string):
        if re.match(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}$', string.lower()):
            return string
        raise argparse.ArgumentTypeError(f'"{string}" is not a valid device address')

    @GObject.Property
    def address(self):
        return self._bluez_device.property('Address')

    @GObject.Property
    def name(self):
        return self._bluez_device.property('Name')

    @GObject.Property
    def listening(self):
        return self.property('Listening')

    @GObject.Property
    def drawings_available(self):
        return self.property('DrawingsAvailable')

    @GObject.Property
    def battery_percent(self):
        return self.property('BatteryPercent')

    @GObject.Property
    def battery_state(self):
        return self.property('BatteryState')

    @GObject.Property
    def connected(self):
        return self._bluez_device.connected

    def _on_connected(self, bluez_device, pspec):
        self.notify('connected')

    def register(self):
        logger.debug(f'{self}: Register')
        # FIXME: Register() doesn't return anything useful yet, so we wait until
        # the device is in the Manager's Devices property
        self.s1 = self.manager.connect('notify::devices', self._on_mgr_devices_updated)
        self.is_registering = True
        self.proxy.Register()

    def start_listening(self):
        self.proxy.StartListening()

    def stop_listening(self):
        try:
            self.proxy.StopListening()
        except GLib.Error as e:
            if (e.domain != 'g-dbus-error-quark' or
                    e.code != Gio.IOErrorEnum.EXISTS or
                    Gio.dbus_error_get_remote_error(e) != 'org.freedesktop.DBus.Error.ServiceUnknown'):
                raise e

    def start_live(self, fd):
        fd_list = Gio.UnixFDList.new()
        fd_list.append(fd)

        res, fds = self.proxy.call_with_unix_fd_list_sync('org.freedesktop.tuhi1.Device.StartLive',
                                                          GLib.Variant('(h)', (fd,)),
                                                          Gio.DBusCallFlags.NO_AUTO_START,
                                                          -1,
                                                          fd_list,
                                                          None)
        if res[0] == 0:
            self.live = True

    def stop_live(self):
        self.proxy.StopLive()
        self.live = False

    def json(self, timestamp):
        SUPPORTED_FILE_FORMAT = 1
        return self.proxy.GetJSONData('(ut)', SUPPORTED_FILE_FORMAT, timestamp)

    def _on_signal_received(self, proxy, sender, signal, parameters):
        if signal == 'ButtonPressRequired':
            logger.info(f'{self}: Press button on device now')
        elif signal == 'ListeningStopped':
            err = parameters[0]
            if err == -errno.EACCES:
                logger.error(f'{self}: wrong device, please re-register.')
            elif err < 0:
                logger.error(f'{self}: an error occured: {os.strerror(-err)}')
            self.notify('listening')

    def _on_properties_changed(self, proxy, changed_props, invalidated_props):
        if changed_props is None:
            return

        changed_props = changed_props.unpack()

        if 'DrawingsAvailable' in changed_props:
            self.notify('drawings-available')
        elif 'Listening' in changed_props:
            self.notify('listening')
        elif 'BatteryPercent' in changed_props:
            self.notify('battery-percent')
        elif 'BatteryState' in changed_props:
            self.notify('battery-state')

    def __repr__(self):
        return f'{self.address} - {self.name}'

    def _on_mgr_devices_updated(self, manager, pspec):
        if not self.is_registering:
            return

        for d in manager.devices:
            if d.address == self.address:
                self.is_registering = False
                self.manager.disconnect(self.s1)
                del(self.s1)
                logger.info(f'{self}: Registration successful')

    def terminate(self):
        try:
            self.manager.disconnect(self.s1)
        except AttributeError:
            pass
        self._bluez_device.terminate()
        super(TuhiKeteDevice, self).terminate()


class TuhiKeteManager(_DBusObject):
    __gsignals__ = {
        'unregistered-device':
            (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    def __init__(self):
        _DBusObject.__init__(self, TUHI_DBUS_NAME,
                             ORG_FREEDESKTOP_TUHI1_MANAGER,
                             ROOT_PATH)

        self._devices = {}
        self._unregistered_devices = {}

        for objpath in self.property('Devices'):
            device = TuhiKeteDevice(self, objpath)
            self._devices[device.address] = device

    @GObject.Property
    def devices(self):
        return [v for k, v in self._devices.items()]

    @GObject.Property
    def unregistered_devices(self):
        return [v for k, v in self._unregistered_devices.items()]

    @GObject.Property
    def searching(self):
        return self.proxy.get_cached_property('Searching')

    def start_search(self):
        self._unregistered_devices = {}
        self.proxy.StartSearch()

    def stop_search(self):
        try:
            self.proxy.StopSearch()
        except GLib.Error as e:
            if (e.domain != 'g-dbus-error-quark' or
                    e.code != Gio.IOErrorEnum.EXISTS or
                    Gio.dbus_error_get_remote_error(e) != 'org.freedesktop.DBus.Error.ServiceUnknown'):
                raise e
        self._unregistered_devices = {}

    def terminate(self):
        for dev in self._devices.values():
            dev.terminate()
        self._devices = {}
        self._unregistered_devices = {}
        super(TuhiKeteManager, self).terminate()

    def _on_properties_changed(self, proxy, changed_props, invalidated_props):
        if changed_props is None:
            return

        changed_props = changed_props.unpack()

        if 'Devices' in changed_props:
            objpaths = changed_props['Devices']
            for objpath in objpaths:
                try:
                    d = self._unregistered_devices[objpath]
                    self._devices[d.address] = d
                    del self._unregistered_devices[objpath]
                except KeyError:
                    # if we called Register() on an existing device it's not
                    # in unregistered devices
                    pass
            self.notify('devices')
        if 'Searching' in changed_props:
            self.notify('searching')

    def _handle_unregistered_device(self, objpath):
        for addr, dev in self._devices.items():
            if dev.objpath == objpath:
                self.emit('unregistered-device', dev)
                return

        device = TuhiKeteDevice(self, objpath)
        self._unregistered_devices[objpath] = device

        logger.debug(f'New unregistered device: {device}')
        self.emit('unregistered-device', device)

    def _on_signal_received(self, proxy, sender, signal, parameters):
        if signal == 'SearchStopped':
            self.notify('searching')
        elif signal == 'UnregisteredDevice':
            objpath = parameters[0]
            self._handle_unregistered_device(objpath)

    def __getitem__(self, btaddr):
        return self._devices[btaddr]


class Worker(GObject.Object):
    '''Implements a command to be executed.
    Subclasses need to overwrite run() that will be executed
    while calling the command.
    Subclass can also implement the stop() method which
    will be executed to terminate the command, once the
    mainloop has finished.'''

    def __init__(self, manager, args=None):
        GObject.GObject.__init__(self)
        self.manager = manager
        self._connected_signals = {}

    def oject_connect(self, obj, signal, callback):
        if signal in self._connected_signals:
            # FIXME: this should be an exception
            logger.error(f'signal {signal} is already set, ignoring')
            return

        s = obj.connect(signal, callback)
        self._connected_signals[signal] = (obj, s)

    def manager_connect(self, signal, callback):
        self.oject_connect(self.manager, signal, callback)

    def cleanup(self):
        for obj, signal in self._connected_signals.values():
            obj.disconnect(signal)
        self._connected_signals = {}

    def run(self):
        pass

    def stop(self):
        pass


class Searcher(Worker):
    def __init__(self, manager, args):
        super(Searcher, self).__init__(manager)
        self.manager_connect('notify::searching', self._on_notify_search)
        self.manager_connect('unregistered-device', self._on_unregistered_device)

    def run(self):
        if self.manager.searching:
            logger.error('Another client is already searching')
            return

        logger.debug(f'Starting searching')
        self.manager.start_search()

    def stop(self):
        if self.manager.searching:
            logger.debug('Stopping search')
            self.manager.stop_search()

        self.cleanup()

    def _on_notify_search(self, manager, pspec):
        if not manager.searching:
            logger.info('Search cancelled')
            self.stop()
        else:
            logger.info('Search started')

    def _on_unregistered_device(self, manager, device):
        logger.info(f'Unregistered device: {device}')


class Listener(Worker):
    def __init__(self, manager, args):
        super(Listener, self).__init__(manager)

        self.device = None
        for d in manager.devices:
            if d.address == args.address:
                self.device = d
                break
        else:
            logger.error(f'{args.address}: device not found')
            # FIXME: this should be an exception
            return

    def device_connect(self, signal, callback):
        self.oject_connect(self.device, signal, callback)

    def run(self):
        if self.device is None:
            return

        if self.device.drawings_available:
            self._log_drawings_available(self.device)

        if self.device.listening:
            logger.info(f'{self.device}: device already listening')
            return

        logger.debug(f'{self.device}: starting listening')
        self.device_connect('notify::listening', self._on_device_listening)
        self.device_connect('notify::drawings-available', self._on_drawings_available)
        self.device.start_listening()

    def stop(self):
        if self.device.listening:
            logger.debug(f'{self.device}: stopping listening')
            self.device.stop_listening()

        self.cleanup()

    def _on_device_listening(self, device, pspec):
        if self.device.listening:
            return

        logger.info(f'{device}: Listening stopped')
        self.stop()

    def _on_drawings_available(self, device, pspec):
        self._log_drawings_available(device)

    def _log_drawings_available(self, device):
        s = ', '.join([f'{t}' for t in device.drawings_available])
        logger.info(f'{device}: drawings available: {s}')


class Fetcher(Worker):
    def __init__(self, manager, args, config):
        super(Fetcher, self).__init__(manager)
        self.device = None
        self.timestamps = None
        address = args.address
        index = args.index

        if address not in config:
            config[address] = {}

        self.orientation = config[address].get('Orientation', 'Landscape')
        self.handle_pressure = config[address].getboolean('HandlePressure', False)

        for d in manager.devices:
            if d.address == address:
                self.device = d
                break
        else:
            logger.error(f'{address}: device not found')
            return

        if index != 'all':
            try:
                index = int(index)
                if index not in self.device.drawings_available:
                    raise ValueError()
                self.timestamps = [index]
            except ValueError:
                logger.error(f'Invalid index {index}')
                return
        else:
            self.timestamps = self.device.drawings_available

    def run(self):
        if self.device is None or self.timestamps is None:
            return

        for ts in self.timestamps:
            jsondata = self.device.json(ts)
            data = json.loads(jsondata)
            t = time.localtime(data['timestamp'])
            t = time.strftime('%Y-%m-%d-%H-%M', t)
            path = f'{data["devicename"]}-{t}.svg'
            self.json_to_svg(data, path)
            logger.info(f'{data["devicename"]}: saved file "{path}"')

    def json_to_svg(self, js, filename):
        dimensions = js['dimensions']
        if dimensions == [0, 0]:
            width, height = 100, 100
        else:
            # Original diemnsions are too big for SVG Standard
            # so we nomalize them
            width, height = dimensions[0] / 100, dimensions[1] / 100

        if self.orientation in ['Portrait', 'Reverse-Portrait']:
            svg = svgwrite.Drawing(filename=filename, size=(height, width))
        else:
            svg = svgwrite.Drawing(filename=filename, size=(width, height))

        g = svgwrite.container.Group(id='layer0')
        for stroke_num, s in enumerate(js['strokes']):

            points_with_sk_width = []

            for p in s['points']:

                x, y = p['position']
                # Normalize coordinates too
                x, y = x / 100, y / 100

                if self.orientation == 'Reverse-Portrait':
                    x, y = y, width - x
                elif self.orientation == 'Portrait':
                    x, y = height - y, x
                elif self.orientation == 'Reverse-Landscape':
                    x, y = width - x, height - y

                delta = (p['pressure'] - 1000.0) / 1000.0
                stroke_width = 0.4 + 0.20 * delta
                points_with_sk_width.append((x, y, stroke_width))

            if self.handle_pressure:
                lines = svgwrite.container.Group(id=f'strokes_{stroke_num}', stroke='black')
                for i, (x, y, stroke_width) in enumerate(points_with_sk_width):
                    if i != 0:
                        xp, yp, stroke_width_p = points_with_sk_width[i - 1]
                        lines.add(
                            svg.line(
                                start=(xp, yp),
                                end=(x, y),
                                stroke_width=stroke_width,
                                style='fill:none'
                            )
                        )
            else:
                lines = svgwrite.path.Path(
                    d="M",
                    stroke='black',
                    stroke_width=0.2,
                    style='fill:none'
                )
                for x, y, stroke_width in points_with_sk_width:
                    lines.push(x, y)

            g.add(lines)

        svg.add(g)
        svg.save()


class LiveChanger(Worker):
    def __init__(self, manager, args):
        super(LiveChanger, self).__init__(manager)

        self.device = None
        for d in manager.devices:
            if d.address == args.address:
                self.device = d
                break
        else:
            logger.error(f'{args.address}: device not found')
            # FIXME: this should be an exception
            return

    def run(self):
        if self.device is None:
            return

        read_fd, write_fd = os.pipe()

        logger.info(f'{self.device}: starting live mode, please press button on device')
        self._cb = GLib.io_add_watch(read_fd, GLib.IO_IN, self._on_uhid_data)
        self.device.start_live(write_fd)

    def _on_uhid_data(self, source, cb_condition):
        buf = os.read(source, 4380)

        header = '< L'
        uhid_type = struct.unpack_from(header, buf)[0]

        if uhid_type == 11:  # UHID_CREATE2
            fmt = '< L 128s 64s 64s H H L L L L 4096s'
            uhid_type, name, phys, uniq, rdesc_size, bus, vid, pid, version, country, rdesc = struct.unpack_from(fmt, buf)
            name = name.rstrip(b'\x00')
            rdesc = rdesc[:rdesc_size]
            logger.info(f'Live mode started for device {name} with rdesc {b2hex(rdesc)}')
        elif uhid_type == 12:  # UHID_INPUT2
            fmt = '< L H 4096s'
            uhid_type, data_len, data = struct.unpack_from(fmt, buf)
            data = data[:data_len]
            logger.info(f'Live data: {b2hex(data)}')

        return True

    def stop(self):
        logger.debug(f'{self.device}: stopping live mode')
        try:
            self.device.stop_live()
        except GLib.Error as e:
            if (e.domain != 'g-dbus-error-quark' or
                    e.code != Gio.IOErrorEnum.EXISTS or
                    Gio.dbus_error_get_remote_error(e) != 'org.freedesktop.DBus.Error.ServiceUnknown'):
                raise e
        GLib.source_remove(self._cb)


class TuhiKeteShellLogHandler(logging.StreamHandler):
    def __init__(self):
        super(TuhiKeteShellLogHandler, self).__init__(sys.stdout)
        self.setFormatter(ColorFormatter(log_format))
        self._prompt = ''

    def emit(self, record):
        self.terminator = f'\n{self._prompt}{readline.get_line_buffer()}'
        super(TuhiKeteShellLogHandler, self).emit(record)

    def set_normal_mode(self):
        self.acquire()
        self.setFormatter(ColorFormatter(log_format))
        self.terminator = '\n'
        self._prompt = ''
        self.release()

    def set_prompt_mode(self, prompt):
        self.acquire()
        # '\x1b[2K\r' clears the current line and start again from the beginning
        self.setFormatter(ColorFormatter(f'\x1b[2K\r{log_format}'))
        self._prompt = prompt
        self.release()


class TuhiKeteShell(cmd.Cmd):
    intro = 'Tuhi shell control'
    prompt = 'tuhi> '

    def __init__(self, completekey='tab', stdin=None, stdout=None):
        super(TuhiKeteShell, self).__init__(completekey, stdin, stdout)
        self._manager = None
        self._workers = []
        self._log_handler = TuhiKeteShellLogHandler()
        logger.removeHandler(logger_handler)
        logger.addHandler(self._log_handler)
        self._log_handler.set_prompt_mode(self.prompt)

        # patching get_names to hide some functions we do not want in the help
        self.get_names = self._filtered_get_names

        try:
            os.mkdir(CONFIG_PATH)
        except FileExistsError:
            pass

        self._config_file = os.path.join(CONFIG_PATH, 'settings.ini')
        self._config = configparser.ConfigParser()
        if os.path.exists(self._config_file):
            self._config.read(self._config_file)
        else:
            # Populate config file with a configuration example
            with open(self._config_file, 'w') as f:
                f.write('''# configuration file for kete

# the file follows a standard .ini format:
# each device should have its own section like the following
# [Bluetooth Address]
# in each section, possible keys are:
# - Orientation (possible values: Portrait, Landscape,
#                                 Reverse-Portrait, Reverse-Landscape
#               defaults to Landscape)
# - HandlePressure (possible values: true, false
#                   defaults to false)


# Example:
[11:22:33:44:55:66]
Orientation = Reverse-Portrait
HandlePressure = true

''')

        self._history_file = os.path.join(CONFIG_PATH, 'histfile')

        try:
            readline.read_history_file(self._history_file)
        except FileNotFoundError:
            readline.write_history_file(self._history_file)

        readline.set_history_length(100)

        Gio.bus_watch_name(Gio.BusType.SESSION,
                           TUHI_DBUS_NAME,
                           Gio.BusNameWatcherFlags.NONE,
                           self._on_name_appeared,
                           self._on_name_vanished)

    def __enter__(self):
        # we can not call GLib.MainLoop() here or it will install a unix signal
        # handler for SIGINT, and we will not be able to catch
        # KeyboardInterrupt in cmdloop()
        self._mainloop = GLib.MainLoop.new(None, False)

        self._glib_thread = threading.Thread(target=self._mainloop.run)
        self._glib_thread.daemon = True
        self._glib_thread.start()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._mainloop.quit()
        self._glib_thread.join()

    def _filtered_get_names(self):
        names = super(TuhiKeteShell, self).get_names()
        names.remove('do_EOF')
        return names

    def _on_name_appeared(self, connection, name, client):
        logger.info('Connected to the Tuhi daemon')
        self._manager = TuhiKeteManager()

    def _on_name_vanished(self, connection, name):
        if self._manager is not None:
            logger.error('Tuhi daemon has quit')
        else:
            logger.warning('Tuhi daemon not running')
        self.terminate_workers()
        if self._manager is not None:
            self._manager.terminate()
        self._manager = None

    def emptyline(self):
        # make sure we do not re-enter the last typed command
        pass

    def do_EOF(self, arg):
        print('\n\r', end='')  # to remove the appended weird char
        return self.do_exit(arg)

    def do_exit(self, args):
        '''Leave the shell'''
        self.terminate_workers()
        return True

    def precmd(self, line):
        # Restore the logger facility to something sane:
        self._log_handler.set_normal_mode()
        if self._manager is None and line not in ['EOF', 'exit', 'help']:
            print('Not connected to the Tuhi daemon')
            return ''

        readline.write_history_file(self._history_file)
        return line

    def postcmd(self, stop, line):
        # overwrite the logger facility to remove the current prompt and append
        # a new one
        self._log_handler.set_prompt_mode(self.prompt)

        # restore any completion display hook we might have set
        readline.set_completion_display_matches_hook()
        return stop

    def run(self, init=None):
        try:
            self.cmdloop(init)
        except KeyboardInterrupt:
            print('^C')
            self.run('')

    def start_worker(self, worker_class, args=None):
        worker = worker_class(self._manager, args)
        worker.run()
        self._workers.append(worker)

    def terminate_worker(self, worker):
        worker.stop()
        self._workers.remove(worker)

    def terminate_workers(self):
        for worker in self._workers:
            worker.stop()
        self._workers = []

    def do_devices(self, arg):
        '''List known devices. These are devices previously registered with
        the daemon.'''
        logger.debug('Listing available devices:')
        for d in self._manager.devices:
            print(d)

    def help_listen(self):
        self.do_listen('-h')

    def complete_listen(self, text, line, begidx, endidx):
        # mark the end of the line so we can match on the number of fields
        if line.endswith(' '):
            line += 'm'
        fields = line.split()

        completion = []
        if len(fields) == 2:
            for device in self._manager.devices:
                if device.address.startswith(text.upper()):
                    completion.append(device.address)
        elif len(fields) == 3:
            for v in ('on', 'off'):
                if v.startswith(text.lower()):
                    completion.append(v)
        return completion

    def do_listen(self, args):
        desc = '''Enable or disable listening on the given device. When
        listening, all drawings are downloaded from the device as they
        device allows connections (this usually requires a button press).
        Drawings are deleted from the device as they are downloaded, they
        are available with the 'fetch' command.
        '''
        parser = argparse.ArgumentParser(prog='listen',
                                         description=desc,
                                         add_help=False)
        parser.add_argument('-h', action='help', help=argparse.SUPPRESS)
        parser.add_argument('address', metavar='12:34:56:AB:CD:EF',
                            type=TuhiKeteDevice.is_device_address,
                            default=None,
                            help='the address of the device to listen to')
        parser.add_argument('mode', choices=['on', 'off'], nargs='?',
                            const='on', default='on')
        try:
            parsed_args = parser.parse_args(args.split())
        except SystemExit:
            return

        address = parsed_args.address
        mode = parsed_args.mode

        for d in self._manager.devices:
            if d.address == address:
                if mode == 'on' and d.listening:
                    print(f'Already listening on {address}')
                    return
                elif mode == 'off' and not d.listening:
                    print(f'Not listening on {address}')
                    return
                break
        else:
            print(f'Device {address} not found')
            return

        if mode == 'off':
            for worker in [w for w in self._workers if isinstance(w, Listener)]:
                if worker.device.address == address:
                    self.terminate_worker(worker)
                    break
            return

        self.start_worker(Listener, parsed_args)

    def help_fetch(self):
        self.do_fetch('-h')

    def complete_fetch(self, text, line, begidx, endidx):

        def draw_timestamp(substitution, matches, longest_match_length):
            print()

            for drawing in matches:
                # we underline the current matching, because it makes easier to
                # visually go through the list
                display_drawing = f'\033[4m{drawing[:len(substitution)]}\033[0m{drawing[len(substitution):]}'

                try:
                    t = time.localtime(int(drawing))
                    t = time.strftime('%Y-%m-%d at %H:%M', t)
                    print(f'{display_drawing}: drawn on the {t}')
                except ValueError:
                    # 'all' case
                    print(f'{display_drawing}{":":<8} fetch all drawings')

            print(self.prompt, readline.get_line_buffer(), sep='', end='')
            sys.stdout.flush()

        # mark the end of the line so we can match on the number of fields
        if line.endswith(' '):
            line += 'm'
        fields = line.split()

        completion = []
        if len(fields) == 2:
            for device in self._manager.devices:
                if device.address.startswith(text.upper()):
                    completion.append(device.address)

        elif len(fields) == 3:
            readline.set_completion_display_matches_hook(draw_timestamp)
            device = None
            for d in self._manager.devices:
                if d.address == fields[1]:
                    device = d
                    break

            if device is None:
                return

            timestamps = [str(t) for t in d.drawings_available]
            timestamps.append('all')

            for t in timestamps:
                if t.startswith(text.lower()):
                    completion.append(t)

        return completion

    def do_fetch(self, args):
        def is_index_or_all(string):
            try:
                n = int(string)
            except ValueError:
                if string == 'all':
                    return string
                raise argparse.ArgumentTypeError(f'"{string}" is neither a timestamp nor "all"')
            else:
                return n

        desc = '''
        Fetches one or all drawings from the given device. These drawings
        must have been previously downloaded from the device (see the
        'listen' command) and are saved in $PWD as SVG files.
        '''
        parser = argparse.ArgumentParser(prog='fetch',
                                         description=desc,
                                         add_help=False)
        parser.add_argument('-h', action='help', help=argparse.SUPPRESS)
        parser.add_argument('address', metavar='12:34:56:AB:CD:EF',
                            type=TuhiKeteDevice.is_device_address,
                            default=None,
                            help='the address of the device to fetch drawing from')
        parser.add_argument('index', metavar='{<index>|all}',
                            type=is_index_or_all,
                            const='all', nargs='?', default='all',
                            help='the index of the drawing to fetch or a literal "all"')

        try:
            parsed_args = parser.parse_args(args.split())
        except SystemExit:
            return

        # we do not call start_worker() as we don't need to retain the
        # worker
        worker = Fetcher(self._manager, parsed_args, self._config)
        worker.run()

    def help_search(self):
        self.do_search('-h')

    def complete_search(self, text, line, begidx, endidx):
        # mark the end of the line so we can match on the number of fields
        if line.endswith(' '):
            line += 'm'
        fields = line.split()

        completion = []
        if len(fields) == 2:
            for v in ('on', 'off'):
                if v.startswith(text.lower()):
                    completion.append(v)

        return completion

    def do_search(self, args):
        desc = '''
        Start/Stop listening for devices that can be registered with the
        daemon. The devices must be in registration mode (blue LED blinking).
        '''
        parser = argparse.ArgumentParser(prog='search',
                                         description=desc,
                                         add_help=False)
        parser.add_argument('-h', action='help', help=argparse.SUPPRESS)
        parser.add_argument('mode', choices=['on', 'off'], nargs='?',
                            const='on', default='on')

        try:
            parsed_args = parser.parse_args(args.split())
        except SystemExit:
            return

        current_searcher = None
        workers = [w for w in self._workers if isinstance(w, Searcher)]
        if len(workers) == 1:
            current_searcher = workers[0]

        if current_searcher is None:
            if parsed_args.mode == 'on':
                self.start_worker(Searcher, parsed_args)
        else:
            if parsed_args.mode == 'off':
                self.terminate_worker(current_searcher)
            else:
                logger.info('Already searching')

    def help_register(self):
        self.do_register('-h')

    def complete_register(self, text, line, begidx, endidx):
        # mark the end of the line so we can match on the number of fields
        if line.endswith(' '):
            line += 'm'
        fields = line.split()

        completion = []
        if len(fields) == 2:
            for device in self._manager.unregistered_devices + self._manager.devices:
                if device.address.startswith(text.upper()):
                    completion.append(device.address)

        return completion

    def do_register(self, args):
        if not self._manager.searching and '-h' not in args.split():
            print('please call search first')
            return

        desc = '''
        Register the given device. The device must be in registration mode
        (blue LED blinking).
        '''
        parser = argparse.ArgumentParser(prog='register',
                                         description=desc,
                                         add_help=False)
        parser.add_argument('-h', action='help', help=argparse.SUPPRESS)
        parser.add_argument('address', metavar='12:34:56:AB:CD:EF',
                            type=TuhiKeteDevice.is_device_address,
                            default=None,
                            help='the address of the device to register')

        try:
            parsed_args = parser.parse_args(args.split())
        except SystemExit:
            return

        address = parsed_args.address

        device = None

        # make sure we do not keep a listener on the device
        for worker in [w for w in self._workers if isinstance(w, Listener)]:
            if worker.device.address == address:
                self.terminate_worker(worker)

        for d in self._manager.devices + self._manager.unregistered_devices:
            if d.address == address:
                device = d
                break
        else:
            logger.error(f'{address}: device not found')
            return

        device.register()

    def help_info(self):
        self.do_info('-h')

    def complete_info(self, text, line, begidx, endidx):
        # mark the end of the line so we can match on the number of fields
        if line.endswith(' '):
            line += 'm'
        fields = line.split()

        completion = []
        if len(fields) == 2:
            for device in self._manager.devices:
                if device.address.startswith(text):
                    completion.append(device.address)

        return completion

    def do_info(self, args):
        desc = '''
        Show information about the given device. If no device is given, show
        information about all known devices'''
        parser = argparse.ArgumentParser(prog='info',
                                         description=desc,
                                         add_help=False)
        parser.add_argument('-h', action='help', help=argparse.SUPPRESS)
        parser.add_argument('address', metavar='12:34:56:AB:CD:EF',
                            type=TuhiKeteDevice.is_device_address,
                            default=None, nargs='?',
                            help='the address of the device to listen to')

        try:
            parsed_args = parser.parse_args(args.split())
        except SystemExit:
            return

        for device in self._manager.devices:
            if parsed_args.address is None or parsed_args.address == device.address:
                print(device)
                charge_strs = {
                    0: 'unknown',
                    1: 'charging',
                    2: 'discharging'
                }
                try:
                    charge_str = charge_strs[device.battery_state]
                except KeyError:
                    charge_str = 'invalid'
                print(f'\tBattery level: {device.battery_percent}%, {charge_str}')
                print('\tAvailable drawings:')
                for d in device.drawings_available:
                    t = time.localtime(d)
                    t = time.strftime('%Y-%m-%d at %H:%M', t)
                    print(f'\t\t* {d}: drawn on the {t}')

    def complete_enable_live(self, text, line, begidx, endidx):
        # mark the end of the line so we can match on the number of fields
        if line.endswith(' '):
            line += 'm'
        fields = line.split()

        completion = []
        if len(fields) == 2:
            for device in self._manager.devices:
                if device.address.startswith(text.upper()):
                    completion.append(device.address)
        elif len(fields) == 3:
            for v in ('on', 'off'):
                if v.startswith(text.lower()):
                    completion.append(v)
        return completion

    def do_enable_live(self, args):
        desc = '''Enable or disable live mode on a particular device'''
        parser = argparse.ArgumentParser(prog='enable_live',
                                         description=desc,
                                         add_help=False)
        parser.add_argument('-h', action='help', help=argparse.SUPPRESS)
        parser.add_argument('address', metavar='12:34:56:AB:CD:EF',
                            type=TuhiKeteDevice.is_device_address,
                            default=None, nargs='?',
                            help='the address of the device to listen to')
        parser.add_argument('mode', choices=['on', 'off'], nargs='?',
                            const='on', default='on')

        try:
            parsed_args = parser.parse_args(args.split())
        except SystemExit:
            return

        address = parsed_args.address
        mode = parsed_args.mode

        for d in self._manager.devices:
            if d.address == address:
                if mode == 'on' and d.live:
                    print(f'Live mode already enabled on {address}')
                    return
                elif mode == 'off' and not d.live:
                    print(f'Live mode not started on  {address}')
                    return
                break
        else:
            print(f'Device {address} not found')
            return

        if mode == 'off':
            for worker in [w for w in self._workers if isinstance(w, LiveChanger)]:
                if worker.device.address == address:
                    self.terminate_worker(worker)
                    break
            return

        self.start_worker(LiveChanger, parsed_args)


def parse(args):
    desc = 'Interactive commandline client to the Tuhi DBus daemon'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('-v', '--verbose',
                        help='Show some debugging informations',
                        action='store_true',
                        default=False)

    return parser.parse_args(args[1:])


def main(args):
    args = parse(args)
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    try:
        with TuhiKeteShell() as shell:
            shell.run()

    except DBusError as e:
        logger.error(e.message)


if __name__ == '__main__':
    main(sys.argv)
