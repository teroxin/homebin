#!/usr/bin/python
import requests
from time import sleep
import json
import ConfigParser
import os


class Light:
    def __init__(self, ip=None, secret=None, lightnum=None, debug=False):
        #If a config is available, default to it
        if os.path.isfile('hue.cfg'):
            config = ConfigParser.RawConfigParser()
            config.read('hue.cfg')
            self.ip = config.get('hue', 'ip')
            self.secret = config.get('hue', 'secret')
            self.lightnum = config.get('hue', 'light')

        #Fill in if parameter was set
        if(ip): self.ip = ip
        if(secret): self.secret = secret
        if(lightnum): self.lightnum = lightnum
        self.debug = debug
        if(not self.secret):
            self.register()

    def register(self):
        secret = None
        while not secret:
            body = json.dumps({'username': 'bettseLight', 'devicetype': 'python'})
            url = 'http://%s/api/' % (self.ip)
            r = requests.post(url, data=body)
            data = json.loads(r.content)[0]
            if(data.has_key('success')):
                secret = data['success']['username']
                print "Key is %s" % secret
            if(data.has_key('error')):
                print "Please push the button on the Phlips Hue Hub"
                sleep(0.5)
        self.secret = secret
        if os.path.isfile('hue.cfg'):
            config = ConfigParser.RawConfigParser()
            config.set('hue', 'secret', secret)
            with open('hue.cfg', 'wb') as configfile:
                config.write(configfile)

    def setstate(self, body):
        if(type(body) != str):
            body = json.dumps(body)
        if(self.debug):
            print "Send %s to light %s" % (body, self.lightnum)
        url = 'http://%s/api/%s/lights/%s/state' % (self.ip, self.secret, self.lightnum)
        r = requests.put(url, data=body)

    def brightness(self, i):
        if(i == 'full'):
            i = 254
        if(int(i) > 254):
            i = 254
        bri = json.dumps({'bri': int(i), 'on': True})
        self.setstate(bri)

    def on(self):
        body = json.dumps({'on': True})
        self.setstate(body)

    def off(self):
        body = json.dumps({'on': False})
        self.setstate(body)

    def number(self):
        return self.lightnum

    def getstate(self):
        url = 'http://%s/api/%s/lights/%s/' % (self.ip, self.secret, self.lightnum)
        r = requests.get(url)
        return json.loads(r.content)['state']

    def colortemp(self, i):
        body = json.dumps({'colormode': 'ct', 'ct': i})
        self.setstate(body)

    def concentrate(self):
        body = json.dumps({u'on': True, u'hue': 13122, u'colormode': u'ct', u'bri': 219, u'sat': 211, u'ct': 233})
        self.setstate(body)

    def energize(self):
        body = json.dumps({u'on': True, u'hue': 13122, u'colormode': u'ct', u'bri': 203, u'sat': 211, u'ct': 156})
        self.setstate(body)

    def reading(self):
        body = json.dumps({u'on': True, u'hue': 13122, u'colormode': u'ct', u'bri': 240, u'sat': 211, u'ct': 346})
        self.setstate(body)

    def relax(self):
        body = json.dumps({u'on': True, u'hue': 13122, u'colormode': u'ct', u'bri': 144, u'sat': 211, u'ct': 467})
        self.setstate(body)

    def red(self):
        self.setstate({"on": True, "hue": 836, "colormode": "xy", "xy": [0.6475, 0.3316]})

    def blue(self):
        self.setstate({"on": True, "hue": 47103, "colormode": "xy", "xy": [0.167, 0.04]})

    def green(self):
        self.setstate({"on": True, "hue": 47103, "colormode": "xy", "xy": [0.3991, 0.4982]})

    def uhwhite(self):
        self.setstate({"on": true, "hue": 47103, "colormode": "xy", "xy": [0.3355, 0.3595]})

