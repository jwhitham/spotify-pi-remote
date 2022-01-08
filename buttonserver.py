
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

# GPIO pin numbers
PIN_B_LED = 7
PIN_R_LED = 21
PIN_G_LED = 20
PIN_B_BUT = 25
PIN_R_BUT = 8
PIN_G_BUT = 16
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
        self.gpio.callback(PIN_B_BUT, pigpio.RISING_EDGE, gpio_event)
        self.gpio.callback(PIN_R_BUT, pigpio.RISING_EDGE, gpio_event)
        self.gpio.callback(PIN_G_BUT, pigpio.RISING_EDGE, gpio_event)
        self.last_tick = 0
        self.last_time = 0.0

    def stop(self) -> None:
        self.gpio.stop()

    def get_state(self) -> State:
        return self.last_state

    def check_cooldown_time(self, current_tick: int) -> bool:
        delta = (current_tick - self.last_tick) & ((1 << 32) - 1)
        self.last_tick = current_tick
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

    def update(self, state: State) -> None:
        self.last_state = state
        b = g = r = 0
        if state == State.STOPPED:
            r = 1
        elif state == State.PLAYING_1:
            g = 1
        elif state == State.PLAYING_2:
            g = 0
        elif state == State.REQUEST:
            b = 1
        elif state == State.ERROR:
            b = g = r = 1

        self.gpio.write(PIN_B_LED, b)
        self.gpio.write(PIN_R_LED, r)
        self.gpio.write(PIN_G_LED, g)

        
def gpio_event(pin: int, level: int, tick: int) -> None:
    with GPIO:
        if GPIO.check_cooldown_time(tick):
            return
        GPIO.update(State.REQUEST)

    update_event(pin, level)

def update_event(pin: int, level: int) -> None:
    try:
        state = State.ERROR
        with SPOTIFY:
            if level == 1:
                if pin == PIN_B_BUT:
                    state = SPOTIFY.press_blue()
                elif pin == PIN_G_BUT:
                    state = SPOTIFY.press_green()
                elif pin == PIN_R_BUT:
                    state = SPOTIFY.press_red()
                else:
                    state = SPOTIFY.press_nothing()
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
            GPIO.update(state)
            
    if update:
        update_event(0, 0)

def periodic_loop() -> None:
    while True:
        periodic_event()
        time.sleep(SHORT_PERIODIC_TIME)

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

    def press_nothing(self) -> State:
        print("press nothing")
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
        if self.path.startswith(REDIRECT_PATH):
            code = self.path[len(REDIRECT_PATH):]
            self.send_response(302, "Found")
            self.send_header("Location", "/")

        elif self.path in ("", "/"):
            self.send_response(200, "OK")

        else:
            self.error()
            return

        self.send_header("Content-type", "html")
        self.end_headers()

        with SPOTIFY:
            try:
                SPOTIFY.get_auth_token(code=code)
                text = "Authentication token is ok"
                state = SPOTIFY.get_state()
                text += " (" + state.name + ")"
            except Exception as x:
                print("Authentication error:" + str(x))
                text = ("Authentication token must be renewed, " +
                    '<a href="' + SPOTIFY.get_authorize_url() +
                    '">click here to log in</a>')

        update_event(0, 0)

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
        HTTPD.serve_forever()
    finally:
        with GPIO:
            GPIO.stop()


