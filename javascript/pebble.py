__author__ = 'katharine'

from array import array
import collections
import logging
import requests
import struct
import traceback
import itertools
from uuid import UUID
import urllib

import PyV8 as v8
from libpebble2.events.threaded import ThreadedEventHandler
from libpebble2.protocol.appmessage import AppMessage
from libpebble2.protocol.system import Model
from libpebble2.services.notifications import Notifications
from libpebble2.services.appmessage import *
from libpebble2.util.hardware import PebbleHardware

import events
from exceptions import JSRuntimeException

logger = logging.getLogger('pypkjs.javascript.pebble')


class TokenException(Exception):
    pass


class Pebble(events.EventSourceMixin, v8.JSClass):
    def __init__(self, runtime, pebble):
        self.extension = v8.JSExtension(runtime.ext_name("pebble"), """
        Pebble = new (function() {
            native function _internal_pebble();
            _make_proxies(this, _internal_pebble(),
                ['sendAppMessage', 'showSimpleNotificationOnPebble', 'getAccountToken', 'getWatchToken',
                'addEventListener', 'removeEventListener', 'openURL', 'getTimelineToken', 'timelineSubscribe',
                'timelineUnsubscribe', 'timelineSubscriptions', 'getActiveWatchInfo']);
            this.platform = 'pypkjs';
        })();
        """, lambda f: lambda: self, dependencies=["runtime/internal/proxy"])
        self.blobdb = pebble.blobdb
        self.pebble = pebble.pebble
        self.runtime = runtime
        self.tid = 0
        self.uuid = UUID(runtime.manifest['uuid'])
        self.app_keys = runtime.manifest['appKeys']
        self.pending_acks = {}
        self.is_ready = False
        self._timeline_token = None
        self._appmessage = AppMessageService(self.pebble, ThreadedEventHandler)
        super(Pebble, self).__init__(runtime)

    def _connect(self):
        self._ready()

    def _ready(self):
        self.is_ready = True
        self._appmessage.register_handler("ack", self._handle_ack)
        self._appmessage.register_handler("nack", self._handle_nack)
        self._appmessage.register_handler("appmessage", self._handle_message)
        self.triggerEvent("ready")

    def _shutdown(self):
        self._appmessage.shutdown()

    def _configure(self):
        self.triggerEvent("showConfiguration")

    def _handle_ack(self, tid, app_uuid):
        self._handle_response(tid, True)

    def _handle_nack(self, tid, app_uuid):
        self._handle_response(tid, False)

    def _handle_response(self, tid, did_succeed):
        try:
            success, failure = self.pending_acks[tid]
        except KeyError:
            return
        callback_param = {"data": {"transactionId": tid}}
        if did_succeed:
            if callable(failure):
                self.runtime.enqueue(success, callback_param)
        else:
            if callable(failure):
                self.runtime.enqueue(failure, callback_param)
        del self.pending_acks[tid]

    def _handle_message(self, tid, uuid, dictionary):
        if uuid != self.uuid:
            logger.warning("Discarded message for %s (expected %s)", uuid, self.uuid)
            self.pebble._send_message("APPLICATION_MESSAGE", struct.pack('<BB', 0x7F, tid))  # ACK
            return

        app_keys = dict(zip(self.app_keys.values(), self.app_keys.keys()))
        d = self.runtime.context.eval("({})")  # This is kinda absurd.
        for k, v in dictionary.iteritems():
            if isinstance(v, int):
                value = v
            elif isinstance(v, basestring):
                value = v
            elif isinstance(v, array):
                value = v8.JSArray(v.tolist())
            else:
                raise JSRuntimeException("?????")
            d[str(k)] = value
            if k in app_keys:
                d[str(app_keys[k])] = value
            e = events.Event(self.runtime, "AppMessage")
            e.payload = d
            self.triggerEvent("appmessage", e)

    def _check_ready(self):
        if not self.is_ready:
            raise JSRuntimeException("Can't interact with the watch before the ready event is fired.")

    def sendAppMessage(self, message, success=None, failure=None):
        self._check_ready()
        to_send = {}
        message = {k: message[str(k)] for k in message.keys()}
        for k, v in message.iteritems():
            if k in self.app_keys:
                k = self.app_keys[k]
            try:
                to_send[int(k)] = v
            except ValueError:
                raise JSRuntimeException("Unknown message key '%s'" % k)

        d = {}
        appmessage = AppMessage()
        for k, v in to_send.iteritems():
            if isinstance(v, v8.JSArray):
                v = ByteArray(list(v))
            if isinstance(v, basestring):
                v = CString(v)
            elif isinstance(v, int):
                v = Int32(v)
            elif isinstance(v, float):  # thanks, javascript
                try:
                    intv = int(round(v))
                except ValueError:
                    self.runtime.log_output("WARNING: illegal float value %s for appmessage key %s" % (v, k))
                    intv = 0
                v = Int32(intv)
            elif isinstance(v, collections.Sequence):
                bytes = []
                for byte in v:
                    if isinstance(byte, int):
                        if 0 <= byte <= 255:
                            bytes.append(byte)
                        else:
                            raise JSRuntimeException("Bytes must be between 0 and 255 inclusive.")
                    elif isinstance(byte, str):  # This is intentionally not basestring; unicode won't work.
                        bytes.extend(list(bytearray(byte)))
                    else:
                        raise JSRuntimeException("Unexpected value in byte array.")
                v = ByteArray(array('B', v))
            elif v is None:
                continue
            else:
                raise JSRuntimeException("Invalid value data type for key %s: %s" % (k, type(v)))
            d[k] = v

        tid = self._appmessage.send_message(self.uuid, d)
        self.pending_acks[tid] = (success, failure)

    def showSimpleNotificationOnPebble(self, title, message):
        self._check_ready()
        Notifications(self.pebble, self.blobdb).send_notification(title, message)

    def showNotificationOnPebble(self, opts):
        pass

    def getAccountToken(self):
        self._check_ready()
        return self.runtime.runner.account_token

    def getWatchToken(self):
        self._check_ready()
        return self.runtime.runner.watch_token

    def openURL(self, url):
        self.runtime.open_config_page(url, self._handle_config_response)

    def _get_timeline_token(self):
        if self._timeline_token is not None:
            return self._timeline_token
        result = requests.get(self.runtime.runner.urls.sandbox_token % self.uuid,
                              headers={'Authorization': 'Bearer %s' % self.runtime.runner.oauth_token})
        if result.status_code == 404:
            raise TokenException("No token available; make sure the app is timeline enabled "
                                 "and this user authorised in the developer portal.")
        elif result.status_code == 401:
            raise TokenException("User login rejected; make sure you are logged in to the SDK.")
        result.raise_for_status()
        logger.debug("get_timeline_token result: %s", result.json())
        self._timeline_token = result.json()['token']
        return self._timeline_token

    def getTimelineToken(self, success=None, failure=None):
        def go():
            try:
                token = self._get_timeline_token()
            except (requests.RequestException, TokenException) as e:
                if callable(failure):
                    self.runtime.enqueue(failure, str(e))
            except Exception:
                traceback.print_exc()
                if callable(failure):
                    self.runtime.enqueue(failure, "Internal failure.")
            else:
                if callable(success):
                    self.runtime.enqueue(success, token)
        self.runtime.group.spawn(go)

    def _do_timeline_thing(self, method, topic, success, failure):
        try:
            token = self._get_timeline_token()
            result = requests.request(method, self.runtime.runner.urls.manage_subscription % urllib.quote(topic, safe=''),
                                      headers={'X-User-Token': token})
            result.raise_for_status()
        except (requests.RequestException, TokenException) as e:
            if callable(failure):
                self.runtime.enqueue(failure, str(e))
        except Exception as e:
            traceback.print_exc()
            if callable(failure):
                self.runtime.enqueue(failure, "Internal failure.")
        else:
            if callable(success):
                self.runtime.enqueue(success)

    def timelineSubscribe(self, topic, success=None, failure=None):
        self.runtime.group.spawn(self._do_timeline_thing, "POST", topic, success, failure)

    def timelineUnsubscribe(self, topic, success=None, failure=None):
        self.runtime.group.spawn(self._do_timeline_thing, "DELETE", topic, success, failure)

    def timelineSubscriptions(self, success=None, failure=None):
        def go():
            try:
                token = self._get_timeline_token()
                result = requests.get(self.runtime.runner.urls.app_subscription_list, headers={'X-User-Token': token})
                result.raise_for_status()
                subs = v8.JSArray(result.json()['topics'])
            except (requests.RequestException, TokenException) as e:
                if callable(failure):
                    self.runtime.enqueue(failure, str(e))
            except Exception:
                traceback.print_exc()
                if callable(failure):
                    self.runtime.enqueue(failure, "Internal failure.")
            else:
                if callable(success):
                    self.runtime.enqueue(success, subs)
        self.runtime.group.spawn(go)

    def getActiveWatchInfo(self):
        watch_info = self.pebble.watch_info

        js_object = self.runtime.context.eval("({})")
        platform = PebbleHardware.hardware_platform(watch_info.running.hardware_platform)
        js_object['platform'] = platform
        model = self.pebble.watch_model  # Note: this could take a while.
        model_map = {
            Model.TintinBlack: "pebble_black",
            Model.TintinRed: "pebble_red",
            Model.TintinWhite: "pebble_white",
            Model.TintinGrey: "pebble_gray",
            Model.TintinOrange: "pebble_orange",
            Model.TintinGreen: "pebble_green",
            Model.TintinPink: "pebble_pink",
            Model.TintinBlue: "pebble_blue",
            Model.BiancaBlack: "pebble_steel_black",
            Model.BiancaSilver: "pebble_steel_silver",
            Model.SnowyWhite: "pebble_time_white",
            Model.SnowyRed: "pebble_time_red",
            Model.SnowyBlack: "pebble_time_black",
        }
        model = model_map.get(platform, 'qemu_platform_%s' % platform)
        js_object['model'] = model
        js_object['language'] = watch_info.language
        firmware_obj = self.runtime.context.eval("({})")
        fw_version = self.pebble.firmware_version
        firmware_obj['major'] = fw_version.major
        firmware_obj['minor'] = fw_version.major
        firmware_obj['patch'] = fw_version.patch
        firmware_obj['suffix'] = fw_version.suffix
        js_object['firmware'] = firmware_obj
        return js_object

    def _handle_config_response(self, response):
        def go():
            e = events.Event(self.runtime, "WebviewClosed")
            e.response = response
            self.triggerEvent("webviewclosed", e)
        self.runtime.enqueue(go)
