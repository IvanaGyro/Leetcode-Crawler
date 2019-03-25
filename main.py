import asyncio
import pyppeteer
from pyppeteer import launch
from pprint import pprint
import json
import time
import math
from datetime import datetime
import configparser
from pathlib import Path

'''
global path setting
'''
BASE_DIR = Path(__file__).parent
CONFIG_PATH = Path(BASE_DIR, 'config.ini')

'''
global datetime setting
'''
DATETIME_FORMAT = '%Y%m%d_%H%M%S'

'''
configuration's key words
'''
SECTION_RECORD = 'Record'
SECTION_USER = 'User'
RECORD_LAST_UPDATE = 'LastUpdate'
USER_USERNAME = 'Username'
USER_PASSWORD = 'Password'

'''
browser setting
'''
MAX_PAGE = 10


filename_extension_table = {
    'javascript': 'js',
    'bash': 'sh'
}


config = None


def init_config():
    global config
    config = configparser.ConfigParser()
    config.optionxform = lambda optionstr: optionstr

    if CONFIG_PATH.is_file():
        config.read(CONFIG_PATH, encoding='utf8')
    else:
        CONFIG_PATH.touch()

    if not config.has_section(SECTION_RECORD):
        config.add_section(SECTION_RECORD)
    if not config.has_section(SECTION_USER):
        config.add_section(SECTION_USER)


def write_config():
    with open(CONFIG_PATH, 'w') as fp:
        config.write(fp)


def get_user():
    update_config = False
    username = config.get(SECTION_USER, USER_USERNAME, fallback='')
    if username == '':
        username = input('Username: ')
        config.set(SECTION_USER, USER_USERNAME, username)
        update_config = True
    password = config.get(SECTION_USER, USER_PASSWORD, fallback='')
    if password == '':
        password = input('Password: ')
        config.set(SECTION_USER, USER_PASSWORD, password)
        update_config = True
    
    if update_config:
        write_config()
    
    return (username, password)
        
    
def get_last_update():
    the_zeroth_datetime = datetime.fromtimestamp(0).strftime(DATETIME_FORMAT)
    last_update_str = config.get(
        SECTION_RECORD,
        RECORD_LAST_UPDATE,
        fallback=the_zeroth_datetime)
    return datetime.strptime(last_update_str,DATETIME_FORMAT).timestamp()


def set_last_update():
    last_update = time.time()
    config.set(SECTION_RECORD, RECORD_LAST_UPDATE,
               datetime.fromtimestamp(last_update).strftime(DATETIME_FORMAT))
    write_config()


'''
hack ot patch the timeout problem of websocket
link: https://github.com/miyakogi/pyppeteer/issues/175
'''


def patch_pyppeteer():
    import pyppeteer.connection
    original_method = pyppeteer.connection.websockets.client.connect

    def new_method(*args, **kwargs):
        kwargs['ping_interval'] = None
        kwargs['ping_timeout'] = None
        return original_method(*args, **kwargs)

    pyppeteer.connection.websockets.client.connect = new_method


patch_pyppeteer()


'''
Catch all the responses of the page
'''
responses = []


async def set_catch_response(page):
    client = await page.target.createCDPSession()
    client.send('Network.enable')

    async def catch_response(event):
        try:
            event['response']['requestBody'] = await client.send('Network.getResponseBody', {'requestId': event['requestId']})
        except Exception as e:
            event['response']['requestBody'] = ''
        responses.append(event)
        # url = event['response']['url']
        # status = event['response']['status']
        # try:
        #     method = event['response']['requestHeaders'][':method']
        # except Exception as e:
        #     print('Can not get the method of the response. Error:{}'.format(e))
        #     return

    client.on('Network.responseReceived', catch_response)


async def main():
    init_config()

    browser = await launch(headless=False)
    page = await browser.newPage()

    async def close():
        print('browser close')
        await browser.close()
        exit()

    await page.goto('https://leetcode.com/accounts/login/',
                    waitUntil='networkidle0')

    # Sometimes cannot catch #initial-loading[data-is-hide="true"] expectly.
    try:
        await page.waitForSelector('#initial-loading[data-is-hide="true"]', timeout=15000)
    except pyppeteer.errors.TimeoutError as e:
        await page.waitForSelector('#username-input', timeout=3000)
        await page.waitForSelector('#password-input', timeout=3000)

    '''
    Login
    '''
    username, password = get_user()
    await page.focus('#username-input')
    await page.keyboard.type(username)
    await page.focus('#password-input')
    await page.keyboard.type(password)
    await page.click('#sign-in-button')

    rspns = await page.waitForResponse(
        lambda rspns: rspns.url == 'https://leetcode.com/accounts/login/' and
        rspns.request.method == 'POST')

    if rspns.status == 200:
        print('login successfully')
    elif rspns.status == 400:
        print('login fail')
        try:
            body = await rspns.text()
            error_msg = json.loads(body)['form']['errors']
            pprint(error_msg)
        except Exception as e:
            print(e)
            print('Cannot get the body of the request')
            body = ''
        await close()
    else:
        print('special status code:{}'. format(rspns.status))
        await close()
    try:
        await page.waitForNavigation(timeout=10)
    except pyppeteer.errors.TimeoutError as e:
        pass

    await page.goto('https://leetcode.com/progress/',
                    waitUntil='domcontentloaded')
    data = await page.evaluate('pageData;')
    total_submissions = data['total_submissions']

    submissions = [None]*total_submissions

    async def get_submissions(offset):
        try:
            page = await browser.newPage()
            # server do not permit sending requests with short interval
            time.sleep(1)
            rspns = await page.goto(
                # The maximum limit is 20
                'https://leetcode.com/api/submissions/?offset={}&limit=20'.format(
                    offset),
                waitUnti='networkidle0')
            body = await rspns.text()
            if rspns.status == 200:
                tmp = json.loads(body)
                submission_profiles = tmp['submissions_dump']
                idx = offset
                for submission in submission_profiles:
                    submissions[idx] = submission
                    idx += 1
            else:
                print('response status:{}. body:{}. offset:{}'.format(
                    rspns.status, body, offset))
            await page.close()
        except Exception as e:
            print('Exception:{}. offset: {}\n'.format(e, offset))

    tasks = [get_submissions(idx*20)
             for idx in range(0, math.ceil(total_submissions/20))]
    result = await asyncio.gather(*tasks)

    for submission in submissions:
        if submission is None:
            print('occur some error')

    rspns = await page.goto('https://leetcode.com/api/problems/all/',
                            waitUntil='networkidle0')
    data = json.loads(await rspns.text())
    questions_table = {}
    max_question_id = 0
    for question in data['stat_status_pairs']:
        title = question['stat']['question__title']
        question_id = question['stat']['question_id']
        # assume one title is only corresponding to one id
        questions_table[title] = question_id
        if question_id > max_question_id:
            max_question_id = question_id

    last_update = get_last_update()
    filtered = [None] * (max_question_id + 1)
    for submission in submissions:
        submit_time = int(submission['timestamp'])
        if submit_time >= last_update:
            question_id = questions_table[submission['title']]
            if filtered[question_id] is not None:
                if submission['status_display'] == 'Accepted':
                    if submit_time > int(filtered[question_id]['timestamp']):
                        submission['question_id'] = question_id
                        filtered[question_id] = submission
            else:
                submission['question_id'] = question_id
                filtered[question_id] = submission

    submissions = [s for s in filtered if s is not None]
    updated_cnt = len(submissions)

    async def get_code():
        page = await browser.newPage()
        while len(submissions):
            submission = submissions.pop()
            retry = 5
            while retry > 0:
                try:
                    url = submission['url']
                    await page.goto('https://leetcode.com{}'.format(submission['url']), waitUntil='domcontentloaded')
                    data = await page.evaluate('pageData;')
                except (pyppeteer.errors.TimeoutError, pyppeteer.errors.NetworkError) as e:
                    retry -= 1
                    if retry == 0:
                        submissions.append(submission)
                    continue
                except Exception as e:
                    print(e)
                    print(submission)
                    break
                code = data['submissionCode']
                id = data['questionId']
                # save code
                time_string = datetime.fromtimestamp(
                    submission['timestamp']).strftime(DATETIME_FORMAT)
                extension = filename_extension_table[submission['lang']]
                filename = './submissions/{}_{}.{}'.format(
                    id, time_string, extension)
                with open(filename, 'wb') as fp:
                    try:
                        fp.write(code.encode('utf8'))
                    except Exception as e:
                        print(e)
                break
        await page.close()

    tasks = [get_code() for i in range(MAX_PAGE)]
    result = await asyncio.gather(*tasks)

    set_last_update()
    if updated_cnt <= 1:
        print('{} submission is updated.'.format(updated_cnt))
    else:
        print('{} submissions are updated.'.format(updated_cnt))

    await close()


asyncio.get_event_loop().run_until_complete(main())

