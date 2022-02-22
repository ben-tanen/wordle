#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta, timezone
import searchtweets
import requests
import time, os, sys, json, math, re
import pytz

#==============================#
# DEFINE MISC HELPER FUNCTIONS #
#==============================#

# parse out parameters of command-line arguments
def parse_argv():
    pattern = "--([A-z0-9]+)=(([A-z0-9]|\.|_|-|/)+)"
    if len(sys.argv) > 1:
        parsed_args_list = [{"key": re.match(pattern, arg)[1],
                             "value": re.match(pattern, arg)[2]} for arg in sys.argv[1:]]
        parsed_args = {obj["key"]: obj["value"] for obj in parsed_args_list}
        return parsed_args
    else:
        return {}

# given two numbers a and b, where a <= b,
# report the proportion of a / b as a loading bar
# primarily for reporting the fill of a list with a known final size
def report_fill(a, b, width = 10, num_width = 3, clear = True):
    if clear:
        sys.stdout.write("\b" * (width + 2 + 4 + num_width * 2))
    else:
        sys.stdout.write("\n")
        sys.stdout.flush()

    fill = int(a / b * width)
    sys.stdout.write("[")
    sys.stdout.write("=" * fill)
    sys.stdout.write(" " * (width - fill))
    sys.stdout.write("] %s / %s" % (str(a).rjust(num_width), str(b).rjust(num_width)))
    sys.stdout.flush()
    
#==============================#
# INIT ENVIRONMENT + MAIN VARS #
#==============================#

# go to proper directory
if os.getcwd()[-6:] != "wordle":
    os.chdir("/Users/ben-tanen/Desktop/Projects/wordle")

# parse command line args
parsed_args = parse_argv()

# enable twitter api auth
st_creds = searchtweets.load_credentials("data/twitter-data/twitter-api-creds.yaml")
bearer_token = st_creds['bearer_token']

# set max allowed tweets to scrape per day
max_tweets_per_month = 2000000
max_tweets_per_day = int(parsed_args['max_tweets']) if 'max_tweets' in parsed_args \
                     else int((max_tweets_per_month / 31) * 0.99)

#=======================#
# DEFINE MAIN FUNCTIONS #
#=======================#

# given a puzzle number or a date,
# return information on the puzzle for that date
# including the range of which tweets should appear
def get_puzzle_details(puzzle = None, date = None):
    init_puzzle = 246
    init_date = datetime.strptime("2022-02-20 +0000", "%Y-%m-%d %z")
    if puzzle:
        new_puzzle = puzzle
        new_date = init_date + timedelta(days = new_puzzle - init_puzzle)
    elif date:
        new_date = datetime.strptime("%s +0000" % date, "%Y-%m-%d %z")
        new_puzzle = init_puzzle + (datetime.strptime("%s +0000" % date, "%Y-%m-%d %z") - init_date).days
    return {
        "date": new_date, 
        "time_range": {
            "start_time": new_date + timedelta(hours = -12),
            "end_time": new_date + timedelta(hours = 24 + 12)
        },
        "puzzle": new_puzzle
    }

# clean the tweets as needed
def clean_tweets(tweets):
    return [{'created_at': pytz.utc.localize(datetime.strptime(tweet['created_at'], "%Y-%m-%dT%H:%M:%S.%fZ")),
             'id': tweet['id'],
             'text': re.sub(r'⬜', '⬛', 
                            re.sub(r'🟦|🟨', '🟨', 
                                   re.sub(r'🟧', '🟩', tweet['text']))).replace(u"\uFE0F", "")
            } for tweet in tweets]

# reverse some of the cleaning operations in order to save to .json
def unclean_tweets(tweets):
    return [{'created_at': tweet['created_at'].strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
             'id': tweet['id'],
             'text': tweet['text']} for tweet in tweets]

# given a mix of parameters, query for wordle puzzle results
def query_wordle_tweets(details, 
                        next_token = None,
                        sort = "recency",
                        max_tweets = max_tweets_per_day,
                        loud = True):
    # construct the url for the Twitter API call
    query_puzzle = details['puzzle']
    query_starttime = details['time_range']['start_time'].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    query_endtime = details['time_range']['end_time'].strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    url_format = "https://api.twitter.com/2/tweets/search/recent?"
    url_format += "query=Wordle%20{puzzle}%20-is%3Aretweet%20-is%3Areply%20-is%3Aquote"
    url_format += "&start_time={starttime}&end_time={endtime}"
    url_format += "&max_results={tweets_per_pull}&sort_order={sort}&tweet.fields=id,created_at,text"
    url = url_format.format(puzzle = query_puzzle, sort = sort, 
                            tweets_per_pull = max(10, min(100, max_tweets)),
                            starttime = query_starttime, endtime = query_endtime)
    
    init_data_raw = requests.get(url + ("&next_token=%s" % next_token if next_token else ""), 
                                 headers = {"Authorization": "Bearer %s" % bearer_token})
    init_data_json = init_data_raw.json()
    
    if init_data_raw.status_code != 200:
        print(init_data_json)
    elif 'data' not in init_data_json.keys():
        print(init_data_json)

    tweets = clean_tweets(init_data_json['data'])
    next_token = init_data_json['meta'].get('next_token')
    time.sleep(2.1)
    while next_token and len(tweets) < max_tweets:
        data_raw = requests.get(url + "&next_token=%s" % next_token, 
                                headers = {"Authorization": "Bearer %s" % bearer_token})
        data_json = data_raw.json()
        
        if data_raw.status_code != 200:
            print(data_json)
        elif 'data' not in data_json.keys():
            print(data_json)
        
        tweets += clean_tweets(data_json['data'])
        if loud:
            report_fill(len(tweets), max_tweets, width = 60, num_width = 5, clear = False)
        next_token = data_json['meta'].get('next_token')
        time.sleep(2.1)
        
    return [tweets, next_token]

# a more complicated version of query_wordle_tweets that will proportionally query throughout the day
def query_wordle_tweets_v2(details,
                           sort = "recency",
                           min_per_tr = 30,
                           max_tweets = max_tweets_per_day,
                           probability_scale = False,
                           loud = True):
    
    # create array of array of 30-min time ranges
    print("calculating time + probability distributions")
    tr = details['time_range']
    trs_init = [tr['start_time'] + timedelta(minutes = dt) for dt in 
                range(0, int((tr['end_time'] - tr['start_time']).total_seconds() / 60) + 1, min_per_tr)]
    trs = [{'start_time': t,
            'end_time': t + timedelta(minutes = min_per_tr, milliseconds = -1)} for t in trs_init]
    trs = [{'start_time': t['start_time'],
            'end_time': t['end_time'],
            'next_token': None,
            'tweets': 0,
            'probability': len([tweet for tweet in all_tweets_puzzle244 \
                                if tweet['created_at'] >= t['start_time'] \
                                and tweet['created_at'] <= t['end_time']]) / \
                           len(all_tweets_puzzle244)} for t in trs]
    
    print("starting to query tweets for puzzle %d" % details['puzzle'])
    tweets = [ ]
    while len(tweets) < max_tweets:
        max_tweets_remaining = max_tweets - len(tweets)
        for ix, tr in enumerate(trs):
            max_tweets_ix = math.ceil(tr['probability'] * max_tweets_remaining * 0.9) if probability_scale else 100
            [survey, next_token] = query_wordle_tweets({'puzzle': details['puzzle'], 'time_range': tr},
                                                       next_token = tr['next_token'],
                                                       max_tweets = max_tweets_ix,
                                                       loud = False)
            tweets += survey
            trs[ix]['next_token'] = next_token
            trs[ix]['tweets'] += len(survey)
            if loud:
                report_fill(len(tweets), max_tweets, width = 60, num_width = 5, clear = True)
            if len(tweets) > max_tweets:
                break       
    print()
    
    return [tweets, trs]   

def get_score(text):
    rms = re.findall(r'ordle {puzzle} ([1-6]|X)/6'.format(puzzle = details['puzzle']), text)
    if len(rms) > 0:
        rm = rms[0]
        if rm == "X":
            return rm
        else:
            return int(rms[0])
    else:
        None

seq_regex = r'(🟩|⬛|🟨){5}'
filter_fxns = [
    # check that the tweet actually contains results sequence
    (lambda t: bool(re.search(seq_regex, t))), 
    
    # check if tweet contains any sketchy keywords
    # (lambda t: not bool(re.search(r'(cheat)', t.lower()))),
    
    # check if reported score matches N results sequences
    (lambda t: len(re.findall(seq_regex, t)) == 6 if get_score(t) == "X" \
               else len(re.findall(seq_regex, t)) == get_score(t))
]

#================================#
# QUERY + SAVE TWEETS FOR PUZZLE #
#================================#

# use distribution of all tweets from puzzle 244 as guide for new puzzle tweet timing distribution
all_tweets_puzzle244 = clean_tweets(json.load(open('data/twitter-data/all-tweets_wordle-244_20220221.json')))

# pull information for puzzle from two days ago
details = get_puzzle_details(puzzle = int(parsed_args['puzzle']) if 'puzzle' in parsed_args else None,
                             date = (datetime.now() + timedelta(days = -2)).strftime("%Y-%m-%d"))
print("pulling tweets for:")
print(details)

# query tweets for specified puzzle
[tweets, trs] = query_wordle_tweets_v2(details, probability_scale = True)

# save sampled tweets (pre-filtering)
with open('data/twitter-data/sampled-tweets_wordle-{puzzle}_{date}.json'.format(puzzle = details['puzzle'],
                                                                                   date = datetime.now().strftime("%Y%m%d")), 
          'w', encoding = 'utf-8') as f:
    json.dump(unclean_tweets(tweets), f, ensure_ascii = False, indent = 4)

# filter tweets based on defined filter_fxns
filtered_tweets = [tweet for tweet in tweets if all(list(map((lambda d: d(tweet['text'])), filter_fxns)))]
n_filtered_out_tweets = len(tweets) - len(filtered_tweets)
print("%d tweets filtered out (%f pct); %d tweets remain" % (n_filtered_out_tweets, 
                                                             n_filtered_out_tweets / len(tweets) * 100,
                                                             len(filtered_tweets)))

# save filtered tweets
with open('data/twitter-data/filtered-tweets_wordle-{puzzle}_{date}.json'.format(puzzle = details['puzzle'],
                                                                                 date = datetime.now().strftime("%Y%m%d")), 
          'w', encoding = 'utf-8') as f:
    json.dump(unclean_tweets(filtered_tweets), f, ensure_ascii = False, indent = 4)