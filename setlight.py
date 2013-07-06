#!/usr/bin/python
import requests
from time import sleep
import json
import ConfigParser
import sys
from light import Light
from os.path import dirname, join


config = ConfigParser.RawConfigParser()
config.read(join(dirname(__file__), 'hue.cfg'))

ip = config.get('hue', 'ip')
secret = config.get('hue', 'secret')
numlights = int(config.get('hue', 'numlights'))
profile = None

def usage():
    print "./setlight.py (all|light#) (full|[0-255]) [relax|reading|concentrate|energize]"
    sys.exit(0)

if(len(sys.argv) < 2):
    usage()

light = sys.argv[1]

bri = int(sys.argv[2])

if(len(sys.argv) > 3):
    profile = sys.argv[3]


if light.strip() == 'all':
    lights = [Light(ip, secret, x, True) for x in range(1, numlights+1)]
else:
    lights = [Light(ip, secret, light, True)]

for light in lights:
    if(profile):
        light.on()
        getattr(light, profile)()
    else:
        if(bri == 0):
            light.off()
        else:
            light.brightness(bri)
