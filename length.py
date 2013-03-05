#!/usr/bin/python

from datetime import datetime, timedelta
then = datetime.strptime('Nov 21 2011', '%b %d %Y')
now = datetime.now()
diff = now - then
years = diff.days/365
diff = diff - timedelta(days=365*years)
months = diff.days/30
diff = diff - timedelta(days=30*months)

print "About %s years, %s months, %s" % (years, months, diff)
