#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
App python
"""
import argparse
import codecs
import errno
import glob
import json
import logging.config
import os
import re
import sys
import textwrap
import time
from operator import itemgetter

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import warnings
import concurrent.futures
from instagram_scraper.utils import extract_tags, get_original_image, set_story_url

import requests
import tqdm

from instagram_scraper.constants import CHROME_WIN_UA, QUERY_COMMENTS, QUERY_LOCATION, SEARCH_URL, QUERY_MEDIA, \
    LOGOUT_URL, QUERY_HASHTAG, VIEW_MEDIA_URL, BASE_URL, STORIES_UA, STORIES_URL, STORIES_COOKIE, LOGIN_URL, USER_URL

warnings.filterwarnings('ignore')


class InstagramScraper(object):
    """InstagramScraper scrapes and downloads an instagram user's photos and videos"""
    login_user = login_pass = login_only = login_only = media_types = include_location = comments = None
    media_metadata = include_location = maximum = quiet = latest = destination = retain_username = None
    usernames = []

    def __init__(self, **kwargs):
        default_attr = dict(username='', usernames=[], filename=None,
                            login_user=None, login_pass=None, login_only=False,
                            destination='./', retain_username=False,
                            quiet=False, maximum=0, media_metadata=False, latest=False,
                            media_types=['image', 'video', 'story'], tag=False, location=False,
                            search_location=False, comments=False, verbose=0, include_location=False, filter=None)

        allowed_attr = list(default_attr.keys())
        default_attr.update(kwargs)

        for key in default_attr:
            if key in allowed_attr:
                self.__dict__[key] = kwargs.get(key)

        # Set up a logger
        self.logger = InstagramScraper.get_logger(level=logging.DEBUG, verbose=default_attr.get('verbose'))

        self.posts = []
        self.session = requests.Session()
        self.session.headers = {'user-agent': CHROME_WIN_UA}

        self.cookies = None
        self.logged_in = False
        self.last_scraped_filemtime = 0
        if default_attr['filter']:
            self.filter = list(self.filter)

    def login(self):
        """Logs in to instagram."""
        self.session.headers.update({'Referer': BASE_URL})
        req = self.session.get(BASE_URL)

        self.session.headers.update({'X-CSRFToken': req.cookies['csrftoken']})

        login_data = {'username': self.login_user, 'password': self.login_pass}
        login = self.session.post(LOGIN_URL, data=login_data, allow_redirects=True)
        self.session.headers.update({'X-CSRFToken': login.cookies['csrftoken']})
        self.cookies = login.cookies
        login_text = json.loads(login.text)

        if login_text.get('authenticated') and login.status_code == 200:
            self.logged_in = True
        else:
            self.logger.error('Login failed for %s', self.login_user)

            if 'checkpoint_url' in login_text:
                self.logger.error('Please verify your account at %s', login_text.get('checkpoint_url'))
            elif 'errors' in login_text:
                for count, error in enumerate(login_text['errors'].get('error')):
                    count += 1
                    self.logger.debug('Session error %(count)s: "%(error)s"', locals())
            else:
                self.logger.error(json.dumps(login_text))

    def logout(self):
        """Logs out of instagram."""
        if self.logged_in:
            try:
                logout_data = {'csrfmiddlewaretoken': self.cookies['csrftoken']}
                self.session.post(LOGOUT_URL, data=logout_data)
                self.logged_in = False
            except requests.exceptions.RequestException:
                self.logger.warning('Failed to log out %s', self.login_user)

    def make_dst_dir(self, username):
        """Creates the destination directory."""
        if self.destination == './':
            dst = './' + username
        else:
            if self.retain_username:
                dst = self.destination + '/' + username
            else:
                dst = self.destination

        try:
            os.makedirs(dst)
        except OSError as err:
            if err.errno == errno.EEXIST and os.path.isdir(dst):
                # Directory already exists
                self.get_last_scraped_filemtime(dst)
                pass
            else:
                # Target dir exists as a file, or a different error
                raise

        return dst

    def get_last_scraped_filemtime(self, dst):
        """Stores the last modified time of newest file in a directory."""
        list_of_files = []
        file_types = ('*.jpg', '*.mp4')

        for type in file_types:
            list_of_files.extend(glob.glob(dst + '/' + type))

        if list_of_files:
            latest_file = max(list_of_files, key=os.path.getmtime)
            self.last_scraped_filemtime = int(os.path.getmtime(latest_file))

    def query_comments_gen(self, shortcode, end_cursor=''):
        """Generator for comments."""
        comments, end_cursor = self.__query_comments(shortcode, end_cursor)

        if comments:
            try:
                while True:
                    for item in comments:
                        yield item

                    if end_cursor:
                        comments, end_cursor = self.__query_comments(shortcode, end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception('Failed to query comments for shortcode ' + shortcode)

    def __query_comments(self, shortcode, end_cursor=''):
        resp = self.session.get(QUERY_COMMENTS.format(shortcode, end_cursor))

        if resp.status_code == 200:
            payload = json.loads(resp.text)['data']['shortcode_media']
            if payload:
                container = payload['edge_media_to_comment']
                comments = [node['node'] for node in container['edges']]
                end_cursor = container['page_info']['end_cursor']
                return comments, end_cursor
        else:
            time.sleep(6)
            return self.__query_comments(shortcode, end_cursor)
        return iter([])

    def scrape_hashtag(self):
        """

        """
        self.__scrape_query(self.query_hashtag_gen)

    def scrape_location(self):
        self.__scrape_query(self.query_location_gen)

    def __scrape_query(self, media_generator, executor=concurrent.futures.ThreadPoolExecutor(max_workers=10)):
        """Scrapes the specified value for posted media."""
        for value in self.usernames:
            self.posts = []
            self.last_scraped_filemtime = 0
            future_to_item = {}

            dst = self.make_dst_dir(value)

            if self.include_location:
                media_exec = concurrent.futures.ThreadPoolExecutor(max_workers=5)

            iteration = 0
            for item in tqdm.tqdm(media_generator(value), desc='Searching {0} for posts'.format(value), unit=" media",
                                  disable=self.quiet):
                if ((item['is_video'] is False and 'image' in self.media_types) or
                        (item['is_video'] is True and 'video' in self.media_types)) and \
                        self.is_new_media(item):
                    future = executor.submit(self.download, item, dst)
                    future_to_item[future] = item

                if self.include_location and 'location' not in item:
                    media_exec.submit(self.__get_location, item)

                if self.comments:
                    item['edge_media_to_comment']['data'] = list(self.query_comments_gen(item['shortcode']))

                if self.media_metadata or self.comments or self.include_location:
                    self.posts.append(item)

                iteration = iteration + 1
                if self.maximum != 0 and iteration >= self.maximum:
                    break

            if future_to_item:
                for future in tqdm.tqdm(concurrent.futures.as_completed(future_to_item), total=len(future_to_item),
                                        desc='Downloading', disable=self.quiet):
                    item = future_to_item[future]

                    if future.exception() is not None:
                        self.logger.warning(
                            'Media for %s at %s generated an exception: %s', value, item['urls'], future.exception())

            if (self.media_metadata or self.comments or self.include_location) and self.posts:
                self.save_json(self.posts, '{0}/{1}.json'.format(dst, value))

    def query_hashtag_gen(self, hashtag):
        return self.__query_gen(QUERY_HASHTAG, 'hashtag', hashtag)

    def query_location_gen(self, location):
        return self.__query_gen(QUERY_LOCATION, 'location', location)

    def __query_gen(self, url, entity_name, query, end_cursor=''):
        """Generator for hashtag and location."""
        nodes, end_cursor = self.__query(url, entity_name, query, end_cursor)

        if nodes:
            try:
                while True:
                    for node in nodes:
                        yield node

                    if end_cursor:
                        nodes, end_cursor = self.__query(url, entity_name, query, end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception('Failed to query ' + query)

    def __query(self, url, entity_name, query, end_cursor):
        resp = self.session.get(url.format(query, end_cursor))

        if resp.status_code == 200:
            payload = json.loads(resp.text)['data'][entity_name]

            if payload:
                nodes = []

                if end_cursor == '':
                    top_posts = payload['edge_' + entity_name + '_to_top_posts']
                    nodes.extend(self._get_nodes(top_posts))

                posts = payload['edge_' + entity_name + '_to_media']

                nodes.extend(self._get_nodes(posts))
                end_cursor = posts['page_info']['end_cursor']
                return nodes, end_cursor
            else:
                return iter([])
        else:
            time.sleep(6)
            return self.__query(url, entity_name, query, end_cursor)

    def _get_nodes(self, container):
        return [self.augment_node(node['node']) for node in container['edges']]

    def augment_node(self, node):
        extract_tags(node)

        details = None
        if self.include_location and 'location' not in node:
            details = self.__get_media_details(node['shortcode'])
            node['location'] = details.get('location') if details else None

        if 'urls' not in node:
            node['urls'] = []

        if node['is_video'] and 'video_url' in node:
            node['urls'] = [node['video_url']]
        elif '__typename' in node and node['__typename'] == 'GraphImage':
            node['urls'] = [get_original_image(node['display_url'])]
        else:
            if details is None:
                details = self.__get_media_details(node['shortcode'])

            if details:
                if '__typename' in details and details['__typename'] == 'GraphVideo':
                    node['urls'] = [details['video_url']]
                elif '__typename' in details and details['__typename'] == 'GraphSidecar':
                    urls = []
                    for carousel_item in details['edge_sidecar_to_children']['edges']:
                        urls += self.augment_node(carousel_item['node'])['urls']
                    node['urls'] = urls
                else:
                    node['urls'] = [get_original_image(details['display_url'])]

        return node

    def __get_media_details(self, shortcode):
        resp = self.session.get(VIEW_MEDIA_URL.format(shortcode))

        if resp.status_code == 200:
            try:
                return json.loads(resp.text)['graphql']['shortcode_media']
            except ValueError:
                self.logger.warning('Failed to get media details for %s', shortcode)

        else:
            self.logger.warning('Failed to get media details for %s', shortcode)

    def __get_location(self, item):
        code = item.get('shortcode', item.get('code'))

        if code:
            details = self.__get_media_details(code)
            item['location'] = details.get('location')

    def scrape(self, executor=concurrent.futures.ThreadPoolExecutor(max_workers=10)):
        """Crawls through and downloads user's media"""
        if self.login_user and self.login_pass:
            self.login()
            if not self.logged_in and self.login_only:
                self.logger.warning('Fallback anonymous scraping disabled')
                return

        for username in self.usernames:
            self.posts = []
            self.last_scraped_filemtime = 0
            future_to_item = {}

            dst = self.make_dst_dir(username)

            # Get the user metadata.
            user = self.fetch_user(username)

            if user:
                self.get_profile_pic(dst, executor, future_to_item, user, username)
                self.get_stories(dst, executor, future_to_item, user, username)

            # Crawls the media and sends it to the executor.
            try:
                user = self.get_user(username)

                if not user:
                    continue

                self.get_media(dst, executor, future_to_item, user)

                # Displays the progress bar of completed downloads. Might not even pop up if all media is downloaded
                # while the above loop finishes.
                if future_to_item:
                    for future in tqdm.tqdm(concurrent.futures.as_completed(future_to_item), total=len(future_to_item),
                                            desc='Downloading', disable=self.quiet):
                        item = future_to_item[future]

                        if future.exception() is not None:
                            self.logger.warning(
                                'Media at %s generated an exception: %s', item['urls'], future.exception())

                if (self.media_metadata or self.comments or self.include_location) and self.posts:
                    self.save_json(self.posts, '{0}/{1}.json'.format(dst, username))
            except ValueError:
                self.logger.error("Unable to scrape user - %s", username)
        self.logout()

    def get_profile_pic(self, dst, executor, future_to_item, user, username):
        # Download the profile pic if not the default.
        if 'image' in self.media_types and 'profile_pic_url_hd' in user \
                and '11906329_960233084022564_1448528159' not in user['profile_pic_url_hd']:
            item = {'urls': [re.sub(r'/[sp]\d{3,}x\d{3,}/', '/', user['profile_pic_url_hd'])],
                    'created_time': 1286323200}

            if self.latest is False or os.path.isfile(dst + '/' + item['urls'][0].split('/')[-1]) is False:
                for item in tqdm.tqdm([item], desc='Searching {0} for profile pic'.format(username), unit=" images",
                                      ncols=0, disable=self.quiet):
                    future = executor.submit(self.download, item, dst)
                    future_to_item[future] = item

    def get_stories(self, dst, executor, future_to_item, user, username):
        """Scrapes the user's stories."""
        if self.logged_in and 'story' in self.media_types:
            # Get the user's stories.
            stories = self.fetch_stories(user['id'])

            # Downloads the user's stories and sends it to the executor.
            iter = 0
            for item in tqdm.tqdm(stories, desc='Searching {0} for stories'.format(username), unit=" media",
                                  disable=self.quiet):
                future = executor.submit(self.download, item, dst)
                future_to_item[future] = item

                iter = iter + 1
                if self.maximum != 0 and iter >= self.maximum:
                    break

    def get_user(self, username):
        """Fetches the user's metadata."""
        url = USER_URL.format(username)

        resp = self.session.get(url)

        if resp.status_code == 200:
            user = json.loads(resp.text)['user']
            if user and user['is_private'] and user['media']['count'] > 0 and not user['media']['nodes']:
                self.logger.error('User %a is private', username)
            return user
        else:
            self.logger.error(
                'Error getting user details for %s. Please verify that the user exists.', username)
        return None

    def get_media(self, dst, executor, future_to_item, user):
        """Scrapes the user's posts for media."""
        if self.media_types == ['story']:
            return

        username = user['username']

        if self.include_location:
            media_exec = concurrent.futures.ThreadPoolExecutor(max_workers=5)

        iter = 0
        for item in tqdm.tqdm(self.query_media_gen(user), desc='Searching {0} for posts'.format(username),
                              unit=' media', disable=self.quiet):
            # -Filter command line
            if self.filter:
                if 'tags' in item:
                    filtered = any(x in item['tags'] for x in self.filter)
                    if self.has_selected_media_types(item) and self.is_new_media(item) and filtered:
                        future = executor.submit(self.download, item, dst)
                        future_to_item[future] = item
                else:
                    # For when filter is on but media doesnt contain tags
                    pass
            # --------------#
            else:
                if self.has_selected_media_types(item) and self.is_new_media(item):
                    future = executor.submit(self.download, item, dst)
                    future_to_item[future] = item

            if self.include_location:
                media_exec.submit(self.__get_location, item)

            if self.comments:
                item['comments'] = {'data': list(self.query_comments_gen(item['shortcode']))}

            if self.media_metadata or self.comments or self.include_location:
                self.posts.append(item)

            iter = iter + 1
            if self.maximum != 0 and iter >= self.maximum:
                break

    def fetch_user(self, username):
        """Fetches the user's metadata."""
        resp = self.session.get(BASE_URL + username)

        if resp.status_code == 200 and '_sharedData' in resp.text:
            try:
                shared_data = resp.text.split("window._sharedData = ")[1].split(";</script>")[0]
                return json.loads(shared_data)['entry_data']['ProfilePage'][0]['user']
            except (TypeError, KeyError, IndexError):
                return None
        return None

    def fetch_stories(self, user_id):
        """Fetches the user's stories."""
        resp = self.session.get(STORIES_URL.format(user_id), headers={
            'user-agent': STORIES_UA,
            'cookie': STORIES_COOKIE.format(self.cookies['ds_user_id'], self.cookies['sessionid'])
        })

        retval = json.loads(resp.text)

        if resp.status_code == 200 and retval['reel'] and 'items' in retval['reel'] and \
                        len(retval['reel']['items']) > 0:
            return [set_story_url(item) for item in retval['reel']['items']]
        return []

    def query_media_gen(self, user, end_cursor=''):
        """Generator for media."""
        media, end_cursor = self.__query_media(user['id'], end_cursor)

        if media:
            try:
                while True:
                    for item in media:
                        if self.latest and self.last_scraped_filemtime >= self.__get_timestamp(item):
                            return
                        yield item

                    if end_cursor:
                        media, end_cursor = self.__query_media(user['id'], end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception('Failed to query media for user %s', user['username'])

    def __query_media(self, id, end_cursor=''):
        resp = self.session.get(QUERY_MEDIA.format(id, end_cursor))

        if resp.status_code == 200:
            payload = json.loads(resp.text)['data']['user']

            if payload:
                container = payload['edge_owner_to_timeline_media']
                nodes = self._get_nodes(container)
                end_cursor = container['page_info']['end_cursor']
                return nodes, end_cursor
        else:
            if resp and resp.text:
                self.logger.warning(resp.text)
            time.sleep(6)
            return self.__query_media(id, end_cursor)

        return iter([])

    def has_selected_media_types(self, item):
        """

        :rtype: object
        """
        filetypes = {'jpg': 0, 'mp4': 0}

        for url in item['urls']:
            ext = self.__get_file_ext(url)
            if ext not in filetypes:
                filetypes[ext] = 0
            filetypes[ext] += 1

        if ('image' in self.media_types and filetypes['jpg'] > 0) or \
                ('video' in self.media_types and filetypes['mp4'] > 0):
            return True

        return False

    def download(self, item, save_dir='./'):
        """Downloads the media file."""
        for url in item['urls']:
            base_name = url.split('/')[-1].split('?')[0]
            file_path = os.path.join(save_dir, base_name)
            is_video = True if 'mp4' in base_name else False

            if not os.path.isfile(file_path):
                with open(file_path, 'wb') as media_file:
                    try:
                        headers = {'Host': urlparse(url).hostname}
                        if is_video:
                            response = self.session.get(url, headers=headers, stream=True)
                            for chunk in response.iter_content(chunk_size=1024):
                                if chunk:
                                    media_file.write(chunk)
                        else:
                            content = self.session.get(url, headers=headers).content
                            media_file.write(content)
                    except requests.exceptions.ConnectionError:
                        time.sleep(5)
                        if is_video:
                            response = self.session.get(url, headers=headers, stream=True)
                            for chunk in response.iter_content(chunk_size=1024):
                                if chunk:
                                    media_file.write(chunk)
                        else:
                            content = self.session.get(url, headers=headers).content
                            media_file.write(content)

                timestamp = self.__get_timestamp(item)
                file_time = int(timestamp if timestamp else time.time())
                os.utime(file_path, (file_time, file_time))

    def is_new_media(self, item):
        """Returns True if the media is new."""
        return self.latest is False or self.last_scraped_filemtime == 0 or (
            'created_time' not in item and 'date' not in item and 'taken_at_timestamp' not in item) or (
                   int(self.__get_timestamp(item)) > self.last_scraped_filemtime)

    @staticmethod
    def __get_timestamp(item):
        return item.get('taken_at_timestamp', item.get('created_time', item.get('taken_at', item.get('date'))))

    @staticmethod
    def __get_file_ext(path):
        return os.path.splitext(path)[1][1:].strip().lower()

    @staticmethod
    def __search(query):
        resp = requests.get(SEARCH_URL.format(query))
        return json.loads(resp.text)

    def search_locations(self):
        query = ' '.join(self.usernames)
        result = self.__search(query)

        if len(result['places']) == 0:
            raise ValueError("No locations found for query '{0}'".format(query))

        sorted_places = sorted(result['places'], key=itemgetter('position'))

        for item in sorted_places[0:5]:
            place = item['place']
            print('location-id: {0}, title: {1}, subtitle: {2}, city: {3}, lat: {4}, lng: {5}'.format(
                place['location']['pk'],
                place['title'],
                place['subtitle'],
                place['location']['city'],
                place['location']['lat'],
                place['location']['lng']
            ))

    @staticmethod
    def save_json(data, dst='./'):
        """Saves the data to a json file."""
        if data:
            with open(dst, 'wb') as file:
                json.dump(data, codecs.getwriter('utf-8')(file), indent=4, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def get_logger(level=logging.DEBUG, verbose=0):
        """Returns a logger."""
        logger = logging.getLogger(__name__)

        file_handler = logging.FileHandler('instagram-scraper.log', 'w')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

        stram_handler = logging.StreamHandler(sys.stdout)
        stram_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        stram_handler_lvls = [logging.ERROR, logging.WARNING, logging.INFO]
        stram_handler.setLevel(stram_handler_lvls[verbose])
        logger.addHandler(stram_handler)

        return logger

    @staticmethod
    def parse_file_usernames(usernames_file):
        """Parses a file containing a list of usernames."""
        users = []

        try:
            with open(usernames_file) as user_file:
                for line in user_file.readlines():
                    # Find all usernames delimited by ,; or whitespace
                    users += re.findall(r'[^,;\s]+', line.split("#")[0])
        except IOError as err:
            raise ValueError('File not found ' + err)

        return users

    @staticmethod
    def parse_delimited_str(input):
        """Parse the string input as a list of delimited tokens."""
        return re.findall(r'[^,;\s]+', input)


def main():
    """

    """
    parser = argparse.ArgumentParser(
        description="instagram-scraper scrapes and downloads an instagram user's photos and videos.",
        epilog=textwrap.dedent("""
        You can hide your credentials from the history, by reading your
        username from a local file:

        $ instagram-scraper @insta_args.txt user_to_scrape

        with insta_args.txt looking like this:
        -u=my_username
        -p=my_password

        You can add all arguments you want to that file, just remember to have
        one argument per line.

        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        fromfile_prefix_chars='@')

    parser.add_argument('username', help='Instagram user(s) to scrape', nargs='*')
    parser.add_argument('--destination', '-d', default='./', help='Download destination')
    parser.add_argument('--login-user', '--login_user', '-u', default=None, help='Instagram login user')
    parser.add_argument('--login-pass', '--login_pass', '-p', default=None, help='Instagram login password')
    parser.add_argument('--login-only', '--login_only', '-l', default=False, action='store_true',
                        help='Disable anonymous fallback if login fails')
    parser.add_argument('--filename', '-f', help='Path to a file containing a list of users to scrape')
    parser.add_argument('--quiet', '-q', default=False, action='store_true', help='Be quiet while scraping')
    parser.add_argument('--maximum', '-m', type=int, default=0, help='Maximum number of items to scrape')
    parser.add_argument('--retain-username', '--retain_username', '-n', action='store_true', default=False,
                        help='Creates username subdirectory when destination flag is set')
    parser.add_argument('--media-metadata', '--media_metadata', action='store_true', default=False,
                        help='Save media metadata to json file')
    parser.add_argument('--include-location', '--include_location', action='store_true', default=False,
                        help='Include location data when saving media metadata')
    parser.add_argument('--media-types', '--media_types', '-t', nargs='+', default=['image', 'video', 'story'],
                        help='Specify media types to scrape')
    parser.add_argument('--latest', action='store_true', default=False, help='Scrape new media since the last scrape')
    parser.add_argument('--tag', action='store_true', default=False, help='Scrape media using a hashtag')
    parser.add_argument('--filter', default=None, help='Filter by tags in user posts', nargs='*')
    parser.add_argument('--location', action='store_true', default=False, help='Scrape media using a location-id')
    parser.add_argument('--search-location', action='store_true', default=False, help='Search for locations by name')
    parser.add_argument('--comments', action='store_true', default=False, help='Save post comments to json file')
    parser.add_argument('--verbose', '-v', type=int, default=0, help='Logging verbosity level')

    args = parser.parse_args()

    if (args.login_user and args.login_pass is None) or (args.login_user is None and args.login_pass):
        parser.print_help()
        raise ValueError('Must provide login user AND password')

    if not args.username and args.filename is None:
        parser.print_help()
        raise ValueError('Must provide username(s) OR a file containing a list of username(s)')
    elif args.username and args.filename:
        parser.print_help()
        raise ValueError('Must provide only one of the following: username(s) OR a filename containing username(s)')

    if args.tag and args.location:
        parser.print_help()
        raise ValueError('Must provide only one of the following: hashtag OR location')

    if args.tag and args.filter:
        parser.print_help()
        raise ValueError('Filters apply to user posts')

    if args.filename:
        args.usernames = InstagramScraper.parse_file_usernames(args.filename)
    else:
        args.usernames = InstagramScraper.parse_delimited_str(','.join(args.username))

    if args.media_types and len(args.media_types) == 1 and re.compile(r'[,;\s]+').findall(args.media_types[0]):
        args.media_types = InstagramScraper.parse_delimited_str(args.media_types[0])

    scraper = InstagramScraper(**vars(args))

    if args.tag:
        scraper.scrape_hashtag()
    elif args.location:
        scraper.scrape_location()
    elif args.search_location:
        scraper.search_locations()
    else:
        scraper.scrape()


if __name__ == '__main__':
    main()
