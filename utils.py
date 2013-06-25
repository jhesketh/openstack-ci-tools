#!/usr/bin/python

import datetime
import git
import json
import mimetypes
import _mysql
import MySQLdb
import os
import shutil
import smtplib
import subprocess
import uuid

from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def get_config():
    # Read config from a file
    with open('/srv/config/gerritevents') as f:
        config = f.read().replace('\n', '')
        return json.loads(config)


def get_cursor():
    """Get a database cursor."""
    flags = get_config()
    db = MySQLdb.connect(user = flags['dbuser'],
                         db = flags['dbname'],
                         passwd = flags['dbpassword'],
                         host = flags['dbhost'])
    cursor = db.cursor(MySQLdb.cursors.DictCursor)
    return cursor


def format_attempt_insert(attempt):
    if attempt is None or attempt == 0:
        return 'null'
    return attempt


def format_attempt_criteria(attempt):
    if attempt is None or attempt == 0:
        return 'is null'
    return '= %s' % attempt


def send_email(subject, mailto, body):
    """Send an email."""

    with open('/srv/config/gerritevents') as f:
        flags = json.loads(f.read())

    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = flags['mailfrom']
    msg['To'] = mailto

    msg.preamble = body
    txt = MIMEText(body, 'plain')
    msg.attach(txt)

    s = smtplib.SMTP(flags['mailserver'])
    s.sendmail(flags['mailfrom'], [mailto], msg.as_string())
    s.quit()


GIT_DIR = '/srv/git'
COW_DIR = '/srv/git-shadow'
VISIBLE_DIR = '/srv/git-checkouts'


def get_patchset_details(cursor, ident, number):
    cursor.execute('select * from patchsets where id="%s" and number=%s;'
                   %(ident, number))
    return cursor.fetchone()


def _calculate_directories(project, refurl):
    safe_refurl = refurl.replace('/', '_')
    git_dir = os.path.join(GIT_DIR, project)
    cow_dir = os.path.join(COW_DIR, project + '_' + safe_refurl)
    visible_dir = os.path.join(VISIBLE_DIR, project + '_' + safe_refurl)
    return (git_dir, cow_dir, visible_dir)


def clone_git(project):
    """Clone a git repo master."""

    proj_elems = project.split('/')
    cmd = ('/srv/openstack-ci-tools/gitclone.sh %s %s'
           %(proj_elems[0], proj_elems[1]))
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    l = p.stdout.readline()
    while l:
        print '%s %s' %(datetime.datetime.now(), l.rstrip())
        l = p.stdout.readline()
    return


def create_git(project, refurl, cursor, worker, ident, number, workname,
               rewind, attempt):
    """Get a safe COW git checkout of the named refurl."""

    conflict = False
    git_dir, cow_dir, visible_dir = _calculate_directories(project, refurl)
    cmd = ('/srv/openstack-ci-tools/gitcheckout.sh "%(visible_dir)s" '
           '"%(project)s" "%(refurl)s" %(rewind)s 2>&1'
           %{'cow_dir': cow_dir,
             'git_dir': git_dir,
             'visible_dir': visible_dir,
             'project': project,
             'refurl': refurl,
             'rewind': rewind})
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    l = p.stdout.readline()
    while l:
        log(cursor, worker, ident, number, workname, attempt, l)
        if l.find('CONFLICT') != -1:
            conflict = True
        l = p.stdout.readline()
    return visible_dir, conflict


def queue_work(cursor, ident, number, workname, attempt=0):
    cursor.execute('insert ignore into work_queue'
                   '(id, number, workname, attempt) values '
                   '("%s", %d, "%s", %s);'
                   %(ident, number, workname,
                     formatted_attempt_insert(attempt)))
    cursor.execute('commit;')


class NoWorkFound(Exception):
    pass


def dequeue_work(cursor, worker):
    selectid = str(uuid.uuid4())
    cursor.execute('update work_queue set selectid="%s", worker="%s", '
                   'heartbeat = NOW() where selectid is NULL limit 1;'
                   %(selectid, worker))
    cursor.execute('commit;')
    cursor.execute('select * from work_queue where selectid="%s";'
                   % selectid)
    if cursor.rowcount == 0:
        raise NoWorkFound()
    row = cursor.fetchone()
    return (row['id'], row['number'], row['workname'], row['attempt'])

def clear_log(cursor, ident, number, workname, attempt):
    cursor.execute('delete from work_logs where id="%s" and number=%s and '
                   'workname="%s" and attempt %s;'
                   %(ident, number, workname,
                     format_attempt_criteria(attempt)))
    cursor.execute('commit;')


def log(cursor, worker, ident, number, workname, attempt, l):
    timestamp = datetime.datetime.now()
    print '%s %s' % (timestamp, l.rstrip())
    batchlog(cursor, worker, ident, number, workname, attempt,
             [(timestamp, l)])


def batchlog(cursor, worker, ident, number, workname, attempt, entries):
    if not os.path.exists('/srv/logs'):
        os.makedirs('/srv/logs')

    logpath=os.path.join('/srv/logs', ident,
                         number + format_attempt_path(attempt) + '.log')

    with open(logpath, 'a+') as f:
        sql = ('insert into work_logs(id, number, workname, worker, log, '
               'timestamp, attempt) values ')
        values = []
        for timestamp, log in entries:
            values.append('("%s", %s, "%s", "%s", "%s", %s, %s)'
                          %(ident, number, workname, worker,
                            _mysql.escape_string(log),
                            datetime_as_sql(timestamp),
                            format_attempt_insert(attempt)))
        f.write('%s %s\n' %(timestamp, log.rstrip()))

    sql += ', '.join(values)
    sql += ';'

    cursor.execute(sql)
    cursor.execute('commit;')

    if len(entries) > 1:
        print '%s Pushed %d log lines to server' %(datetime.datetime.now(),
                                                   len(entries))
    heartbeat(cursor, worker, ident, number, workname, attempt)


def heartbeat(cursor, worker, ident, number, workname, attempt):
    cursor.execute('update work_queue set heartbeat=NOW() where '
                   'id="%s" and number=%s and workname="%s" and '
                   'worker="%s" and attempt %s;'
                   %(ident, number, workname, worker,
                     format_attempt_criteria(attempt)))
    cursor.execute('commit;')


def datetime_as_sql(value):
    return ('STR_TO_DATE("%s", "%s")'
            %(value.strftime('%a, %d %b %Y %H:%M:%S'),
              '''%a, %d %b %Y %H:%i:%s'''))
