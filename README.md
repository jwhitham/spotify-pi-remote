# spotify-pi-remote

This Python program allows remote access to some simple
Spotify functions, namely play/pause/previous and next track.

The intended usage is to run the Spotify app elsewhere, e.g. on a PC, 
and then use this remote control to quickly skip tracks you don't
like and replay ones that you do. At my house it is attached
to a Concept 2 rowing machine.

For detailed setup instructions, see setup.txt.

The RPi has no buttons of its own, so some hardware
needs to be built and connected to the RPi's pins. This consists
of three buttons and (ideally) three LEDs. However, the software
does not require anything to be connected to the GPIO, and can be
tested via a web browser.


