
# See README.md for an overview of this project.
# See setup.txt for detailed instructions.


from spotipy.oauth2 import SpotifyOAuth  # type: ignore
from spotipy.client import Spotify  # type: ignore
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import pigpio  # type: ignore
import enum
import typing
import time
import json
import socket


# Load the configuration (see setup.txt)
CONFIG = json.load(open(os.path.expanduser("~/.buttonserver.conf")))
CLIENT_ID = CONFIG["client_id"]
CLIENT_SECRET = CONFIG["client_secret"]
REDIRECT_URI = CONFIG["redirect"]
CACHE_PATH = os.path.abspath(os.path.expanduser("~/.spotipy.cache"))
PORT_NUMBER = int(CONFIG["port"])

REDIRECT_PATH = "/" + REDIRECT_URI.rpartition("/")[2] + "?code="
SCOPES = "user-modify-playback-state,user-read-playback-state"
LONG_PERIODIC_TIME = 300.0 # poll interval for Spotify status (seconds)
SHORT_PERIODIC_TIME = 1.0 # flashing LED time
DEBOUNCE_TIME = 200000 # debounce time (microseconds)
NOTIFICATION_IP = CONFIG.get("notification_ip", "")
NOTIFICATION_PORT = int(CONFIG.get("notification_port", 0))

# GPIO pin numbers
PIN_B_LED = 7
PIN_R_LED = 21
PIN_G_LED = 20
PIN_B_BUT = 25
PIN_R_BUT = 8
PIN_G_BUT = 16

# Special "pins"
PIN_NONE = -1
PIN_ALL = -2

# Wire          Physical    GPIO    Button  Purpose
# ----          --------    ----    ------  -------
# Red           2           n/a     n/a     +5V
# Blue          22          25      Blue    Button
# White         24          8       Red     Button
# Grey          26          7       Blue    LED
# Brown         30          n/a     n/a     GND
# Green         36          16      Green   Button
# Purple        38          20      Green   LED
# Yellow        40          21      Red     LED
#


class State(enum.Enum):
    STOPPED = enum.auto()
    PLAYING_1 = enum.auto()
    PLAYING_2 = enum.auto()
    ERROR = enum.auto()
    REQUEST = enum.auto()
    ALL_PRESSED_1 = enum.auto()
    ALL_PRESSED_2 = enum.auto()
    ALL_PRESSED_3 = enum.auto()


class MyGPIOConnection(threading.Semaphore):
    def __init__(self) -> None:
        threading.Semaphore.__init__(self)
        self.last_state = State.REQUEST
        self.gpio = pigpio.pi()
        self.gpio.set_mode(PIN_B_LED, pigpio.OUTPUT)
        self.gpio.set_mode(PIN_G_LED, pigpio.OUTPUT)
        self.gpio.set_mode(PIN_R_LED, pigpio.OUTPUT)
        self.gpio.set_mode(PIN_B_BUT, pigpio.INPUT)
        self.gpio.set_mode(PIN_G_BUT, pigpio.INPUT)
        self.gpio.set_mode(PIN_R_BUT, pigpio.INPUT)
        self.gpio.set_pull_up_down(PIN_B_BUT, pigpio.PUD_UP)
        self.gpio.set_pull_up_down(PIN_G_BUT, pigpio.PUD_UP)
        self.gpio.set_pull_up_down(PIN_R_BUT, pigpio.PUD_UP)
        self.update(State.REQUEST)
        self.gpio.callback(PIN_B_BUT, pigpio.FALLING_EDGE, gpio_event)
        self.gpio.callback(PIN_R_BUT, pigpio.FALLING_EDGE, gpio_event)
        self.gpio.callback(PIN_G_BUT, pigpio.FALLING_EDGE, gpio_event)
        self.last_tick: typing.Dict[int, int] = dict()
        self.last_time = 0.0

    def stop(self) -> None:
        self.gpio.stop()

    def get_state(self) -> State:
        return self.last_state

    def check_cooldown_time(self, pin: int, current_tick: int) -> bool:
        delta = (current_tick - self.last_tick.get(pin, 0)) & ((1 << 32) - 1)
        self.last_tick[pin] = current_tick
        if delta > DEBOUNCE_TIME:
            # accept (not cooldown)
            return False
        else:
            # reject (cooldown)
            return True

    def check_long_periodic_time(self) -> bool:
        current_time = time.time()
        delta = current_time - self.last_time
        if delta > LONG_PERIODIC_TIME:
            self.last_time = current_time
            return True
        else:
            return False

    def is_multi_press(self) -> bool:
        count = 0
        if not self.gpio.read(PIN_B_BUT):
            count += 1
        if not self.gpio.read(PIN_R_BUT):
            count += 1
        if not self.gpio.read(PIN_G_BUT):
            count += 1
        return count >= 2

    def update(self, state: State) -> None:
        self.last_state = state
        b = g = r = 0
        if state == State.STOPPED:
            r = 1
        elif state == State.PLAYING_1:
            g = 1
        elif state == State.PLAYING_2:
            g = 0
        elif state == State.ALL_PRESSED_1:
            r = g = 1
        elif state == State.ALL_PRESSED_2:
            g = b = 1
        elif state == State.ALL_PRESSED_3:
            b = r = 1
        elif state == State.REQUEST:
            b = 1
        elif state == State.ERROR:
            b = g = r = 1

        self.gpio.write(PIN_B_LED, b)
        self.gpio.write(PIN_R_LED, r)
        self.gpio.write(PIN_G_LED, g)

        
def gpio_event(pin: int, level: int, tick: int) -> None:
    with GPIO:
        if GPIO.is_multi_press():
            pin = PIN_ALL
        if GPIO.check_cooldown_time(pin, tick):
            return
        GPIO.update(State.REQUEST)

    update_event(pin)

def update_event(pin: int) -> None:
    try:
        state = State.ERROR
        with SPOTIFY:
            if pin == PIN_B_BUT:
                state = SPOTIFY.press_blue()
                notify("press blue")
            elif pin == PIN_G_BUT:
                state = SPOTIFY.press_green()
                notify("press green")
            elif pin == PIN_R_BUT:
                state = SPOTIFY.press_red()
                notify("press red")
            elif pin == PIN_ALL:
                state = SPOTIFY.press_all()
                notify("press all")
            else:
                state = SPOTIFY.press_nothing()

        with GPIO:
            GPIO.update(state)

    except Exception as x:
        print("Button error: " + str(x))
        with GPIO:
            GPIO.update(State.ERROR)

def periodic_event() -> None:
    with GPIO:
        state = GPIO.get_state()
        update = GPIO.check_long_periodic_time()
        if not update:
            if state == State.PLAYING_2:
                state = State.PLAYING_1
            elif state == State.PLAYING_1:
                state = State.PLAYING_2
            elif state == State.ALL_PRESSED_2:
                state = State.ALL_PRESSED_3
            elif state == State.ALL_PRESSED_3:
                state = State.ALL_PRESSED_1
            elif state == State.ALL_PRESSED_1:
                state = State.ALL_PRESSED_2
            GPIO.update(state)
            
    if update:
        update_event(PIN_NONE)
        notify(state.name.lower())

def periodic_loop() -> None:
    while True:
        periodic_event()
        time.sleep(SHORT_PERIODIC_TIME)

def notify(msg: str) -> None:
    if (not NOTIFICATION_PORT) or (not NOTIFICATION_IP):
        return

    target = (NOTIFICATION_IP, NOTIFICATION_PORT)
    msg1 = msg.encode("ascii")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.sendto(msg1, target)
    except Exception as e:
        print("notify('{}') error: {}".format(msg, e))

class MySpotifyConnection(threading.Semaphore):
    def __init__(self) -> None:
        threading.Semaphore.__init__(self)
        self.auth_manager = SpotifyOAuth(
                        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                        open_browser=False, cache_path=CACHE_PATH,
                        scope=SCOPES, redirect_uri=REDIRECT_URI)

    def get_authorize_url(self) -> str:
        return str(self.auth_manager.get_authorize_url())

    def get_auth_token(self, code: str = "invalid") -> typing.Any:
        return self.auth_manager.get_access_token(as_dict=False, code=code)

    def factory(self) -> Spotify:
        return Spotify(auth=self.get_auth_token(),
                       auth_manager=self.auth_manager)

    def get_state(self, s: typing.Optional[Spotify]=None) -> State:
        try:
            if s is None:
                s = self.factory()
            cp = s.currently_playing()
        except Exception:
            return State.ERROR

        if cp is None:
            return State.STOPPED
        if not cp.get("is_playing", False):
            return State.STOPPED

        return State.PLAYING_1

    def press_blue(self) -> State:
        print("press blue")
        try:
            s = self.factory()
            s.previous_track()
            state = self.get_state(s)
            if state == State.STOPPED:
                state = State.PLAYING_1
            return state
        except Exception:
            return State.ERROR

    def press_red(self) -> State:
        print("press red")
        try:
            s = self.factory()
            state = self.get_state(s)
            if state == State.STOPPED:
                s.start_playback()
                state = State.PLAYING_1
            elif state in (State.PLAYING_1, State.PLAYING_2):
                s.pause_playback()
                state = State.STOPPED
        except Exception as x:
            print("Pause/unpause error:", x)
            state = State.ERROR

        return state

    def press_green(self) -> State:
        print("press green")
        try:
            s = self.factory()
            s.next_track()
            state = self.get_state(s)
            if state == State.STOPPED:
                state = State.PLAYING_1
            return state
        except Exception:
            return State.ERROR

    def press_all(self) -> State:
        print("press all")
        try:
            s = self.factory()
            s.pause_playback()
        except Exception as x:
            print("Pause/unpause error:", x)
        return State.ALL_PRESSED_1

    def press_nothing(self) -> State:
        try:
            s = self.factory()
            return self.get_state(s)
        except Exception:
            return State.ERROR

SPOTIFY = MySpotifyConnection()
GPIO = MyGPIOConnection()


class MyHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        code = "invalid"
        redirect = True
        if self.path.startswith(REDIRECT_PATH):
            code = self.path[len(REDIRECT_PATH):]

        elif self.path == "/blue":
            with SPOTIFY:
                SPOTIFY.press_blue()

        elif self.path == "/red":
            with SPOTIFY:
                SPOTIFY.press_red()

        elif self.path == "/green":
            with SPOTIFY:
                SPOTIFY.press_red()

        elif self.path == "/nothing":
            with SPOTIFY:
                SPOTIFY.press_nothing()

        elif self.path in ("", "/"):
            redirect = False
            self.send_response(200, "OK")

        else:
            self.error()
            return

        if redirect:
            self.send_response(302, "Found")
            self.send_header("Location", "/")
        self.send_header("Content-type", "html")
        self.end_headers()

        with SPOTIFY:
            state = State.ERROR
            try:
                SPOTIFY.get_auth_token(code=code)
                text = "Spotify authentication token is ok"
                state = SPOTIFY.get_state()
            except Exception as x:
                print("Authentication error:" + str(x))
                text = ("Spotify authentication token must be renewed, " +
                    '<a href="' + SPOTIFY.get_authorize_url() +
                    '">click here to log in</a>')

            if state != State.ERROR:
                text += ("<br/>"
                    + "Current state: " + state.name
                    + '<br/><a href="/nothing">Refresh</a>'
                    + '<br/><a href="/blue">(BLUE) Previous</a>'
                    + '<br/><a href="/red">(RED) Play/pause</a>'
                    + '<br/><a href="/green">(GREEN) Next track</a>'
                    )

        update_event(PIN_NONE)

        text = "<html><body>" + text + "</body></html>"
        self.wfile.write(text.encode("ascii"))

    def do_HEAD(self) -> None:
        self.error()

    def do_POST(self) -> None:
        self.error()

    def error(self) -> None:
        self.send_response(404, "Not found")
        self.send_header("Content-type", "html")
        self.end_headers()


if __name__ == "__main__":
    try:
        HTTPD = HTTPServer(('', PORT_NUMBER), MyHTTPRequestHandler)
        PERIODIC_THREAD = threading.Thread(target=periodic_loop, daemon=True)
        PERIODIC_THREAD.start()
        notify("booted")
        HTTPD.serve_forever()
    finally:
        with GPIO:
            GPIO.stop()


