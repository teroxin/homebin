#! /opt/local/bin/python

from BeautifulSoup import BeautifulSoup
import urllib2
import sys, time, os

if(len(sys.argv) > 1):
    pkg = sys.argv[0]
else:
    pkg = "431947684274"

while 1:
    page = urllib2.urlopen("http://www.fedex.com/Tracking?action=track&language=english&cntry_code=us&initial=x&tracknumbers=" +  pkg)
    soup = BeautifulSoup(page)

    detail = soup.find("div", "detailshipmentstatus")
    status = detail.find("div", "bigstatus")

    print "%s at %s" % (status.contents[0], time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))

    if(status.contents[0] != "In transit"):
        os.system('say "Package Delivered"')

    time.sleep(600)
