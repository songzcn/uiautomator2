#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, print_function

import hashlib
import time
import datetime
import functools
import json
import io
import xml.dom.minidom
import xml.etree.ElementTree as ET
import threading

import six
from PIL import Image
import humanize
from subprocess import list2cmdline

if six.PY2:
    import urlparse
else: # for py3
    import urllib.parse as urlparse

import requests


DEBUG = False


class UiaError(Exception):
    pass

class JsonRpcError(UiaError):
    @staticmethod
    def format_errcode(errcode):
        m = {
            -32700: 'Parse error',
            -32600: 'Invalid Request',
            -32601: 'Method not found',
            -32602: 'Invalid params',
            -32603: 'Internal error',
        }
        if errcode in m:
            return m[errcode]
        if errcode >= -32099 and errcode <= -32000:
            return 'Server error'
        return 'Unknown error'

    def __init__(self, error):
        self.code = error.get('code')
        self.message = error.get('message')
        self.data = error.get('data')
        self.exception_name = self.data and self.data.get('exceptionTypeName', '')

    def __str__(self):
        return '%d %s: %s' % (
            self.code,
            self.format_errcode(self.code),
            self.message)
    
    def __repr__(self):
        return repr(str(self))


class SessionBrokenError(UiaError):
    pass

class UiObjectNotFoundError(JsonRpcError):
    pass


def log_print(s):
    thread_name = threading.current_thread().getName()
    print(thread_name + ": " + datetime.datetime.now().strftime('%H:%M:%S,%f')[:-3] + " " + s)


def U(x):
    if six.PY3:
        return x
    return x.decode('utf-8') if type(x) is str else x


def stringfy_jsonrpc_errcode(errcode):
    m = {
        -32700: 'Parse error',
        -32600: 'Invalid Request',
        -32601: 'Method not found',
        -32602: 'Invalid params',
        -32603: 'Internal error',
    }
    if errcode in m:
        return m[errcode]
    if errcode >= -32099 and errcode <= -32000:
        return 'Server error'
    return 'Unknown error'


def connect(addr='127.0.0.1'):
    """
    Args:
        addr (str): uiautomator server address
    
    Example:
        connect("10.0.0.1")
    """
    if '://' not in addr:
        addr = 'http://' + addr
    if addr.startswith('http://'):
        u = urlparse.urlparse(addr)
        host = u.hostname
        port = u.port or 7912
        return AutomatorServer(host, port)
    else:
        raise RuntimeError("address should startswith http://")


class AutomatorServer(object):
    def __init__(self, host, port=7912):
        self._host = host
        self._port = port
        self._reqsess = requests.Session() # use HTTP Keep-Alive to speed request
        self._server_url = 'http://{}:{}'.format(host, port)
        self._server_jsonrpc_url = self._server_url + "/jsonrpc/0"
        self._default_session = Session(self, None)

    def path2url(self, path):
        return urlparse.urljoin(self._server_url, path)

    @property
    def jsonrpc(self):
        """
        Make jsonrpc call easier
        For example:
            self.jsonrpc.pressKey("home")
        """
        class JSONRpcWrapper():
            def __init__(self, server):
                self.server = server
                self.method = None
            
            def __getattr__(self, method):
                self.method = method
                return self

            def __call__(self, *args, **kwargs):
                params = args if args else kwargs
                return self.server.jsonrpc_call(self.method, params)
        
        return JSONRpcWrapper(self)

    def jsonrpc_call(self, method, params=[]):
        """ jsonrpc2 call
        Refs:
            - http://www.jsonrpc.org/specification
        """
        data = {
            "jsonrpc": "2.0",
            "id": self._jsonrpc_id(method),
            "method": method,
            "params": params,
        }
        data = json.dumps(data).encode('utf-8')
        res = self._reqsess.post(self._server_jsonrpc_url,
            headers={"Content-Type": "application/json"},
            timeout=60,
            data=data)
        if DEBUG:
            print("Shell$ curl -X POST -d '{}' {}".format(data, self._server_jsonrpc_url))
            print("Output> " + res.text)
        if res.status_code != 200:
            raise UiaError(self._server_jsonrpc_url, data, res.status_code, res.text, "HTTP Return code is not 200")
        jsondata = res.json()
        error = jsondata.get('error')
        if not error:
            return jsondata.get('result')

        # error happends
        code, data = error.get('code'), error.get('data')
        if -32099 <= code <= -32000: # Server error
            exceptionName = data and data.get('exceptionTypeName', '')
            if 'UiObjectNotFoundException' in exceptionName:
                raise UiObjectNotFoundError(repr(message))
        raise JsonRpcError(error)
    
    def _jsonrpc_id(self, method):
        m = hashlib.md5()
        m.update(("%s at %f" % (method, time.time())).encode("utf-8"))
        return m.hexdigest()
    
    def touch_action(self, x, y):
        """
        Returns:
            TouchAction
        """
        raise NotImplementedError()
    
    def app_install(self, url):
        """
        {u'message': u'downloading', u'id': u'2', u'titalSize': 407992690, u'copiedSize': 49152}
        """
        r = self._reqsess.post(self.path2url('/install'), data={'url': url})
        id = r.text.strip()
        interval = 1.0 # 2.0s
        next_refresh = time.time()
        while True:
            if time.time() < next_refresh:
                time.sleep(.2)
                continue
            ret = self._reqsess.get(self.path2url('/install/'+id))
            progress = ret.json()
            total_size = progress.get('totalSize') or progress.get('titalSize')
            copied_size = progress.get('copiedSize')
            message = progress.get('message')
            if message == 'downloading':
                next_refresh = time.time() + interval
            elif message == 'installing':
                next_refresh = time.time() + interval*2
            log_print("{} {} / {}".format(
                progress.get('message'),
                humanize.naturalsize(copied_size),
                humanize.naturalsize(total_size)))
            if progress.get('error'):
                raise RuntimeError(progress.get('error'), progress.get('message'))
            if message == 'success installed':
                break
        return True
    
    def dump_hierarchy(self, compressed=False, pretty=False):
        content = self.jsonrpc.dumpWindowHierarchy(compressed, None)
        if pretty and "\n " not in content:
            xml_text = xml.dom.minidom.parseString(content.encode("utf-8"))
            content = U(xml_text.toprettyxml(indent='  '))
        return content

    def adb_shell(self, *args):
        """
        Example:
            adb_shell('pwd')
            adb_shell('ls', '-l')
            adb_shell('ls -l')

        Returns:
            shell output
        """
        cmdline = args[0] if len(args) == 1 else list2cmdline(args)
        ret = self._reqsess.post(self.path2url('/shell'), data={'command': cmdline})
        if ret.status_code != 200:
            raise RuntimeError("expect status 200, but got %d" % ret.status_code)
        return ret.json().get('output')
    
    def app_start(self, pkg_name, activity=None):
        """ Launch application """
        # self.adb_shell('pm clear com.netease.index')
        if activity is None:
            self.adb_shell('monkey', '-p', pkg_name, '-c', 'android.intent.category.LAUNCHER', '1')
        else:
            self.adb_shell('am', 'start', '-n', '{}/{}'.format(pkg_name, activity))
    
    def app_stop(self, pkg_name):
        """ Stop application """
        self.adb_shell('am', 'force-stop', pkg_name)
    
    def app_clear(self, pkg_name):
        self.adb_shell('pm', 'clear', pkg_name)
    
    @property
    def screenshot_uri(self):
        return 'http://%s:%d/screenshot/0' % (self._host, self._port)

    def session(self, pkg_name):
        """
        Context context = InstrumentationRegistry.getInstrumentation().getContext();
        Intent intent = context.getPackageManager().getLaunchIntentForPackage(YOUR_APP_PACKAGE_NAME);
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK);
        context.startActivity(intent);

        It is also possible to get pid, and use pid to get package name
        """
        raise NotImplementedError()

    def dismiss_apps(self):
        """
        UiDevice.getInstance().pressRecentApps();
        UiObject recentapp = new UiObject(new UiSelector().resourceId("com.android.systemui:id/dismiss_task"));
        """
        raise NotImplementedError()
        self.press("recent")
    
    def __getattr__(self, attr):
        return getattr(self._default_session, attr)

    def __call__(self, **kwargs):
        return self._default_session(**kwargs)


def check_alive(fn):
    @functools.wraps(fn)
    def inner(self, *args, **kwargs):
        if not self._check_alive():
            raise SessionBrokenError(self._pkg_name)
        return fn(self, *args, **kwargs)
    return inner


class Session(object):
    __orientation = (  # device orientation
        (0, "natural", "n", 0),
        (1, "left", "l", 90),
        (2, "upsidedown", "u", 180),
        (3, "right", "r", 270)
    )

    def __init__(self, server, pkg_name):
        self.server = server
        self._pkg_name = pkg_name

    def _check_alive(self):
        return True

    @property
    @check_alive
    def jsonrpc(self):
        return self.server.jsonrpc

    def tap(self, x, y):
        """
        Tap position
        """
        return self.jsonrpc.click(x, y)

    def click(self, x, y):
        """
        Alias of tap
        """
        return self.tap(x, y)
    
    def long_click(self, x, y, duration=0.5):
        '''long click at arbitrary coordinates.'''
        return self.swipe(x, y, x + 1, y + 1, duration)
    
    def swipe(self, fx, fy, tx, ty, duration=0.5):
        """
        Args:
            fx, fy: from position
            tx, ty: to position
            duration (float): duration
        
        Documents:
            uiautomator use steps instead of duration
            As the document say: Each step execution is throttled to 5ms per step.
        
        Links:
            https://developer.android.com/reference/android/support/test/uiautomator/UiDevice.html#swipe%28int,%20int,%20int,%20int,%20int%29
        """
        return self.jsonrpc.swipe(fx, fy, tx, ty, int(duration*200))
    
    def swipePoints(self, points, duration=0.5):
        ppoints = []
        for p in points:
            ppoints.append(p[0])
            ppoints.append(p[1])
        return self.jsonrpc.swipePoints(ppoints, int(duration)*200)

    def drag(self, sx, sy, ex, ey, duration=0.5):
        '''Swipe from one point to another point.'''
        return self.jsonrpc.drag(sx, sy, ex, ey, int(duration*200))

    def screenshot(self, filename=None):
        """
        Image format is PNG
        """
        r = requests.get(self.server.screenshot_uri)
        if filename:
            with open(filename, 'wb') as f:
                f.write(r.content)
            return filename
        else:
            buff = io.BytesIO(r.content)
            return Image.open(buff)

    def freeze_rotation(self, freeze=True):
        '''freeze or unfreeze the device rotation in current status.'''
        self.jsonrpc.freezeRotation(freeze)

    
    def press(self, key, meta=None):
        """
        press key via name or key code. Supported key name includes:
            home, back, left, right, up, down, center, menu, search, enter,
            delete(or del), recent(recent apps), volume_up, volume_down,
            volume_mute, camera, power.
        """
        if isinstance(key, int):
            return self.jsonrpc.pressKeyCode(key, meta) if meta else self.server.jsonrpc.pressKeyCode(key)
        else:
            return self.jsonrpc.pressKey(key)
    
    def screen_on(self):
        self.jsonrpc.wakeUp()
    
    def screen_off(self):
        self.jsonrpc.sleep()

    @property
    def orientation(self):
        '''
        orienting the devie to left/right or natural.
        left/l:       rotation=90 , displayRotation=1
        right/r:      rotation=270, displayRotation=3
        natural/n:    rotation=0  , displayRotation=0
        upsidedown/u: rotation=180, displayRotation=2
        '''
        return self.__orientation[self.info["displayRotation"]][1]

    def set_orientation(self, value):
        '''setter of orientation property.'''
        for values in self.__orientation:
            if value in values:
                # can not set upside-down until api level 18.
                self.jsonrpc.setOrientation(values[1])
                break
        else:
            raise ValueError("Invalid orientation.")

    # @orientation.setter
    # def orientation(self, value):
    
    @property
    def last_traversed_text(self):
        '''get last traversed text. used in webview for highlighted text.'''
        return self.jsonrpc.getLastTraversedText()

    def clear_traversed_text(self):
        '''clear the last traversed text.'''
        self.jsonrpc.clearLastTraversedText()
    
    def open_notification(self):
        return self.jsonrpc.openNotification()

    def open_quick_settings(self):
        return self.jsonrpc.openQuickSettings()

    def exists(self, **kwargs):
        return self(**kwargs).exists

    def xpath_findall(self, xpath):
        xml = self.server.dump_hierarchy()
        root = ET.fromstring(xml)
        return root.findall(xpath)

    @property
    def info(self):
        return self.jsonrpc.deviceInfo()

    def __call__(self, **kwargs):
        return UiObject(self, Selector(**kwargs))


def wait_exists_wrap(fn):
    @functools.wraps(fn)
    def inner(self, *args, **kwargs):
        self.wait(timeout=self.wait_timeout)
        return fn(self, *args, **kwargs)
    return inner


class UiObject(object):
    def __init__(self, session, selector):
        self.session = session
        self.selector = selector
        self.jsonrpc = session.jsonrpc
        self.wait_timeout = 20

    @property
    def exists(self):
        '''check if the object exists in current window.'''
        return self.jsonrpc.exist(self.selector)

    @property
    def info(self):
        '''ui object info.'''
        return self.jsonrpc.objInfo(self.selector)

    @wait_exists_wrap
    def tap(self):
        '''
        click on the ui object.
        Usage:
        d(text="Clock").click()  # click on the center of the ui object
        '''
        return self.jsonrpc.click(self.selector)

    def click(self):
        """ Alias of tap """
        return self.tap()
    
    @wait_exists_wrap
    def long_click(self):
        info = self.info
        if info['longClickable']:
            return self.jsonrpc.longClick(self.selector)
        bounds = info.get("visibleBounds") or info.get("bounds")
        x = (bounds["left"] + bounds["right"]) / 2
        y = (bounds["top"] + bounds["bottom"]) / 2
        return self.session.long_click(x, y)

    @wait_exists_wrap
    def drag_to(self, *args, **kwargs):
        duration = kwargs.pop('duration', 0.5)
        steps = int(duration*200)
        if len(args) >= 2 or "x" in kwargs or "y" in kwargs:
            def drag2xy(x, y):
                return self.jsonrpc.dragTo(self.selector, x, y, steps)
            return drag2xy(*args, **kwargs)
        return self.jsonrpc.dragTo(self.selector, Selector(**kwargs), steps)

    def wait(self, exists=True, timeout=10.0):
        """
        Wait until UI Element exists or gone
        
        Example:
            d(text="Clock").wait()
            d(text="Settings").wait("gone") # wait until it's gone
        """
        if exists:
            return self.jsonrpc.waitForExists(self.selector, int(timeout*1000))
        else:
            return self.jsonrpc.waitUntilGone(self.selector, int(timeout*1000))
    
    def wait_gone(self, timeout=10.0):
        """ wait until ui gone """
        return self.wait(exists=False)
    
    @wait_exists_wrap
    def set_text(self, text):
        if not text:
            return self.jsonrpc.clearTextField(self.selector)
        else:
            return self.jsonrpc.setText(self.selector, text)
    
    @wait_exists_wrap
    def clear_text(self):
        return self.set_text(None)

    def child(self, **kwargs):
        return UiObject(
            self.session,
            self.selector.clone().child(**kwargs)
        )

    def sibling(self, **kwargs):
        return UiObject(
            self.session, 
            self.selector.clone().sibling(**kwargs)
        )
    
    def __getitem__(self, index):
        selector = self.selector.clone()
        selector['instance'] = index
        return UiObject(self.session, selector)    


class Selector(dict):
    """The class is to build parameters for UiSelector passed to Android device.
    """
    __fields = {
        "text": (0x01, None),  # MASK_TEXT,
        "textContains": (0x02, None),  # MASK_TEXTCONTAINS,
        "textMatches": (0x04, None),  # MASK_TEXTMATCHES,
        "textStartsWith": (0x08, None),  # MASK_TEXTSTARTSWITH,
        "className": (0x10, None),  # MASK_CLASSNAME
        "classNameMatches": (0x20, None),  # MASK_CLASSNAMEMATCHES
        "description": (0x40, None),  # MASK_DESCRIPTION
        "descriptionContains": (0x80, None),  # MASK_DESCRIPTIONCONTAINS
        "descriptionMatches": (0x0100, None),  # MASK_DESCRIPTIONMATCHES
        "descriptionStartsWith": (0x0200, None),  # MASK_DESCRIPTIONSTARTSWITH
        "checkable": (0x0400, False),  # MASK_CHECKABLE
        "checked": (0x0800, False),  # MASK_CHECKED
        "clickable": (0x1000, False),  # MASK_CLICKABLE
        "longClickable": (0x2000, False),  # MASK_LONGCLICKABLE,
        "scrollable": (0x4000, False),  # MASK_SCROLLABLE,
        "enabled": (0x8000, False),  # MASK_ENABLED,
        "focusable": (0x010000, False),  # MASK_FOCUSABLE,
        "focused": (0x020000, False),  # MASK_FOCUSED,
        "selected": (0x040000, False),  # MASK_SELECTED,
        "packageName": (0x080000, None),  # MASK_PACKAGENAME,
        "packageNameMatches": (0x100000, None),  # MASK_PACKAGENAMEMATCHES,
        "resourceId": (0x200000, None),  # MASK_RESOURCEID,
        "resourceIdMatches": (0x400000, None),  # MASK_RESOURCEIDMATCHES,
        "index": (0x800000, 0),  # MASK_INDEX,
        "instance": (0x01000000, 0)  # MASK_INSTANCE,
    }
    __mask, __childOrSibling, __childOrSiblingSelector = "mask", "childOrSibling", "childOrSiblingSelector"

    def __init__(self, **kwargs):
        super(Selector, self).__setitem__(self.__mask, 0)
        super(Selector, self).__setitem__(self.__childOrSibling, [])
        super(Selector, self).__setitem__(self.__childOrSiblingSelector, [])
        for k in kwargs:
            self[k] = kwargs[k]

    def __setitem__(self, k, v):
        if k in self.__fields:
            super(Selector, self).__setitem__(U(k), U(v))
            super(Selector, self).__setitem__(self.__mask, self[self.__mask] | self.__fields[k][0])
        else:
            raise ReferenceError("%s is not allowed." % k)

    def __delitem__(self, k):
        if k in self.__fields:
            super(Selector, self).__delitem__(k)
            super(Selector, self).__setitem__(self.__mask, self[self.__mask] & ~self.__fields[k][0])

    def clone(self):
        kwargs = dict((k, self[k]) for k in self
                      if k not in [self.__mask, self.__childOrSibling, self.__childOrSiblingSelector])
        selector = Selector(**kwargs)
        for v in self[self.__childOrSibling]:
            selector[self.__childOrSibling].append(v)
        for s in self[self.__childOrSiblingSelector]:
            selector[self.__childOrSiblingSelector].append(s.clone())
        return selector

    def child(self, **kwargs):
        self[self.__childOrSibling].append("child")
        self[self.__childOrSiblingSelector].append(Selector(**kwargs))
        return self

    def sibling(self, **kwargs):
        self[self.__childOrSibling].append("sibling")
        self[self.__childOrSiblingSelector].append(Selector(**kwargs))
        return self

    child_selector, from_parent = child, sibling