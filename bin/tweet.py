#!/usr/bin/env python
# encoding: utf8
from __future__ import absolute_import, division, unicode_literals, print_function
from gratipay import wireup
from gratipay.elsewhere.twitter import Twitter

import requests


env = wireup.env()

twitter = Twitter(
    env.twitter_consumer_key,
    env.twitter_consumer_secret,
    env.twitter_callback,
)

paydays = requests.get('https://gratipay.com/about/charts.json').json()
npayday = len(paydays)
nusers = int(paydays[0]['active_users'])
volume = int(round(float(paydays[0]['weekly_gifts'])))


status = "d whit537 Gratipay {npayday:,}—{nusers:,} users exchanged ${volume:,}: " \
         "https://gratipay.com/about/stats."
status = status.format(npayday=npayday, nusers=nusers, volume=volume)

print("Really tweet the following as @Gratipay?")
print("=" * 78)
print()
print(status)
print()
print("=" * 78)

proceed = raw_input("(y/N) ") == 'y'

if not proceed:
    print("Cancelled.")
    raise SystemExit

print("Okay! Here we go ...")
sess = twitter.get_auth_session()
response = sess.post('https://api.twitter.com/1.1/statuses/update.json', {"status": status})
import pdb; pdb.set_trace()
