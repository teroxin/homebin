#!/usr/bin/python

from datetime import datetime, timedelta
import inflect
p = inflect.engine()

then = datetime.strptime('Nov 21 2011', '%b %d %Y')
now = datetime.now()
diff = now - then
years = diff.days/365
diff = diff - timedelta(days=365*years)
months = diff.days/30
diff = diff - timedelta(days=30*months)
days = diff.days

grammar = {'years': p.plural_noun('year', years),
        'months': p.plural_noun('month', months),
        'days': p.plural_noun('day', days)}

print "It has been about %s %s, %s %s, %s %s" % (years, grammar['years'], months, grammar['months'], days, grammar['days'])
