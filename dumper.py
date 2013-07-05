#!/usr/bin/python

# Dump logs of jobs to a web server

import cgi
import datetime
import json
import os
import re

import utils


# Remember that the timestamp isn't actually part of the log row!
UPGRADE_BEGIN_RE = re.compile('\*+ DB upgrade to state of (.*) starts \*+')
UPGRADE_END_RE = re.compile('\*+ DB upgrade to state of (.*) finished \*+')

GIT_CHECKOUT_RE = re.compile('/srv/git-checkouts/[a-z]+/'
                             '[a-z]+_refs_changes_[0-9_]+')
VENV_PATH_RE = re.compile('/home/mikal/\.virtualenvs/refs_changes_[0-9_]+')

MIGRATION_START_RE = re.compile('([0-9]+) -&gt; ([0-9]+)\.\.\.$')
MIGRATION_END_RE = re.compile('^done$')

FINAL_VERSION_RE = re.compile('Final schema version is ([0-9]+)')
MIGRATION_CLASH_RE = re.compile('Error: migration number .* appears '
                                'more than once')

NEW_RESULT_EMAIL = """Results for a test are available.

    %(results)s

"""


def timedelta_as_str(delta):
    seconds = delta.days * (24 * 60 * 60)
    seconds += delta.seconds

    if seconds < 60:
        return '%d seconds' % seconds

    remainder = seconds % 60
    return '%d minutes, %d seconds' %((seconds - remainder) / 60,
                                      remainder)


def test_name_as_display(test):
    return test.replace('sqlalchemy_migration_nova', 'nova upgrade').\
                replace('_', ' ')


def write_index(sql, filename):
    # Write out an index file
    order = []
    test_names = []
    cursor.execute(sql)
    for row in cursor:
        key = (row['id'], row['number'])

        if not key in order:
            order.append(key)
        if not row['workname'] in test_names:
            test_names.append(row['workname'])

    with open(filename, 'w') as f:
        cursor.execute('select count(*) from patchsets;')
        total = cursor.fetchone()['count(*)']
        cursor.execute('select count(*) from patchset_rechecks;')
        rechecks = cursor.fetchone()['count(*)']

        cursor.execute('select timestamp from patchsets order by '
                       'timestamp desc limit 1;')
        recent = cursor.fetchone()['timestamp']
        cursor.execute('select count(*) from work_queue where done="y";')
        jobs_done = cursor.fetchone()['count(*)']
        cursor.execute('select count(*) from work_queue where done is null;')
        jobs_queued = cursor.fetchone()['count(*)']

        f.write('<html><head><title>Recent tests</title></head><body>\n'
                '<p>This page lists recent CI tests run by this system.</p>\n'
                '<p>There are currently %(total)s patchsets tracked and '
                '%(retries)s rechecks, with %(jobs_done)s jobs having been '
                'run. There are %(jobs_queued)s jobs queued to run. The most '
                'recent patchset is from %(recent)s. This page was last '
                'updated at %(now)s.</p>'
                '<table><tr><td><b>Patchset</b></td>'
                %{'total': total,
                  'retries': rechecks,
                  'jobs_done': jobs_done,
                  'jobs_queued': jobs_queued,
                  'recent': recent,
                  'now': datetime.datetime.now()})

        test_names.sort()
        for test in test_names:
            f.write('<td><b>%s</b></td>' % test_name_as_display(test))
        f.write('</tr>\n')

        row_colors = ['', ' bgcolor="#CCCCCC"']
        row_count = 0
        for key in order:
            cursor.execute('select * from patchsets where id="%s" and '
                           'number=%s order by timestamp desc limit 1;'
                           %(key[0], key[1]))
            row = cursor.fetchone()
            f.write('<tr%(color)s><td>'
                    '<a href="%(id)s/%(num)s">%(id)s #%(num)s</a><br/>'
                    '<font size="-1">%(proj)s at %(timestamp)s<br/>'
                    '<a href="%(url)s">%(subj)s by %(who)s</a><br/>'
                    '</font></td>'
                    % {'color': row_colors[row_count % 2],
                       'id': key[0],
                       'num': key[1],
                       'proj': row['project'],
                       'timestamp': row['timestamp'],
                       'subj': row['subject'],
                       'who': row['owner_name'],
                       'url': row['url']})
            for test in test_names:
                test_dir = os.path.join('/var/www/ci', key[0], str(key[1]),
                                        test)

                # Find attempts
                attempt = 0
                while os.path.exists(test_dir +
                                     utils.format_attempt_path(attempt)):
                    attempt += 1
                attempt -= 1

                if attempt > 0:
                    test_dir += utils.format_attempt_path(attempt)

                if os.path.exists(test_dir):
                    with open(os.path.join(test_dir, 'data'), 'r') as d:
                        data = json.loads(d.read())
                    color = data.get('color', '')
                    f.write('<td %s><a href="%s/%s/%s%s/log.html">log</a>'
                            '<font size="-1">'
                            %(color, key[0], key[1], test,
                              utils.format_attempt_path(attempt)))

                    if data.get('result', ''):
                        f.write('<br/><b>%s</b><br/>'
                                % data.get('result', ''))

                    for upgrade in data['order']:
                        f.write('<br/>%s: %s' %(upgrade,
                                                data['details'][upgrade]))

                    if data.get('final_schema_version', ''):
                        f.write('<br/>Final schema version: %s'
                                % data.get('final_schema_version'))
                    if data.get('expected_final_schema_version', ''):
                        f.write('<br/>Expected schema version: %s'
                                % data.get('expected_final_schema_version'))

                    cursor.execute('select * from work_queue where id="%s" '
                                   'and number=%s and workname="%s";'
                                   %(key[0], key[1], test))
                    row = cursor.fetchone()
                    f.write('<br/>Run at %s' % row['heartbeat'])

                    if attempt > 0:
                        f.write('<br/><br/>Other attempts: ')
                        for i in range(0, attempt):
                            f.write('<a href="%s/%s/%s%s/log.html">%s</a> '
                                    %(key[0], key[1], test,
                                      utils.format_attempt_path(i), i))

                    f.write('</font></td>')
                else:
                    f.write('<td>&nbsp;</td>')
            f.write('</tr>\n')
            row_count += 1
        f.write('</table></body></html>')


if __name__ == '__main__':
    print '...'

    cursor = utils.get_cursor()
    subcursor = utils.get_cursor()
    subsubcursor = utils.get_cursor()

    # Write out individual work logs
    cursor.execute('select * from work_queue where done is not null;')
    for row in cursor:
        path = os.path.join('/var/www/ci', row['id'], str(row['number']),
                            row['workname'])
        path += utils.format_attempt_path(row['attempt'])

        datapath = os.path.join(path, 'data')
        workerpath = os.path.join(path, 'worker')
        worker = None
        if os.path.exists(workerpath):
            with open(workerpath, 'r') as f:
                worker = f.read().rstrip()

        if worker != row['worker']:
            print path
            if not os.path.exists(path):
                os.makedirs(path)
            with open(workerpath, 'w') as f:
                f.write(row['worker'])
            with open(os.path.join(path, 'state'), 'w') as f:
                f.write(row['done'])
            with open(os.path.join(path, 'log.html'), 'w') as f:
                buffered = []
                upgrades = []
                upgrade_times = {}
                in_upgrade = False
                migration_start = None
                final_version = None

                subcursor.execute('select * from work_logs where id="%s" and '
                                  'number=%s and workname="%s" and '
                                  'worker="%s" and %s order by timestamp asc;'
                                  %(row['id'], row['number'], row['workname'],
                                    row['worker'],
                                    utils.format_attempt_criteria(
                                        row['attempt'])))
                linecount = 0
                f.write('<html><head><title>%(id)s -- %(number)s</title>\n'
                        '<link rel="stylesheet" type="text/css" '
                        'href="/style.css" />\n'
                        '</head><body>\n'
                        '<h1>CI run for %(id)s, patchset %(number)s</h1>\n'
                        '<p>What is this? This page shows the logs from a '
                        'database upgrade continuous integration run. Each '
                        'patchset which proposes a database migration is run '
                        'against a set of test databases. This page shows the '
                        'results for one of those test databases. If the '
                        'database is from Folsom, you will see a Grizzly '
                        'migration in the bullet list below. You should then '
                        'see an upgrade to the current state of trunk, and '
                        'then finally the upgrade(s) contained in the '
                        'patchset. For more information, please contact '
                        '<a href="mailto:mikal@stillhq.com">'
                        'mikal@stillhq.com</a>.</p>\n'
                        % {'id': row['id'],
                           'number': row['number']})

                data = {}
                for logrow in subcursor:
                    m = FINAL_VERSION_RE.match(logrow['log'])
                    if m:
                         final_version = int(m.group(1))

                    m = UPGRADE_BEGIN_RE.match(logrow['log'])
                    if m:
                         upgrade_name = m.group(1)
                         upgrades.append(upgrade_name)
                         upgrade_start = logrow['timestamp']
                         in_upgrade = True

                         buffered.append('<a name="%s"></a>' % upgrade_name)

                    m = MIGRATION_CLASH_RE.match(logrow['log'])
                    if m:
                        data['color'] = 'bgcolor="#FA5858"'
                        data['result'] = 'Failed: migration number clash'
                        print '    Failed'

                    line = ('<a name="%(linenum)s"></a>'
                            '<a href="#%(linenum)s">#</a> '
                            % {'linenum': linecount})
                    if in_upgrade:
                        line += '<b>'

                    cleaned = logrow['log'].rstrip()
                    cleaned = cleaned.replace('/srv/openstack-ci-tools', '...')
                    cleaned = GIT_CHECKOUT_RE.sub('...git...', cleaned)
                    cleaned = VENV_PATH_RE.sub('...venv...', cleaned)
                    cleaned = cgi.escape(cleaned)

                    m = MIGRATION_END_RE.match(cleaned)
                    if m and migration_start:
                        elapsed = logrow['timestamp'] - migration_start
                        cleaned += ('              <font color="red">[%s]'
                                    '</font>'
                                    % timedelta_as_str(elapsed))
                        migration_start = None

                    m = MIGRATION_START_RE.match(cleaned)
                    if m:
                        migration_start = logrow['timestamp']
                        subsubcursor.execute('select * from '
                                             'patchset_migrations '
                                             'where id="%s" and number=%s and '
                                             'migration=%s;'
                                             %(row['id'], row['number'],
                                               m.group(2)))
                        subsubrow = subsubcursor.fetchone()
                        if subsubrow:
                            cleaned += ('     <font color="red">[%s]</font>'
                                        % subsubrow['name'])

                    line += ('%(timestamp)s %(line)s'
                             % {'timestamp': logrow['timestamp'],
                                'line': cleaned})
                    if in_upgrade:
                        line += '</b>'
                    line += '\n'
                    buffered.append(line)

                    linecount += 1

                    m = UPGRADE_END_RE.match(logrow['log'])
                    if m:
                         in_upgrade = False
                         elapsed = logrow['timestamp'] - upgrade_start
                         elapsed_str = timedelta_as_str(elapsed)
                         buffered.append('                                   '
                                         '     <font color="red"><b>'
                                         '[%s total]</b></font>\n'
                                          % elapsed_str)
                         upgrade_times[upgrade_name] = elapsed

                display_upgrades = []
                data.update({'order': upgrades,
                             'details' : {},
                             'details_seconds': {},
                             'final_schema_version': final_version})
                for upgrade in upgrades:
                    time_str = timedelta_as_str(upgrade_times[upgrade])
                    display_upgrades.append('<li><a href="#%(name)s">'
                                            'Upgrade to %(name)s -- '
                                            '%(elapsed)s</a>'
                                            % {'name': upgrade,
                                               'elapsed': time_str})
                    data['details'][upgrade] = time_str
                    data['details_seconds'][upgrade] = \
                        upgrade_times[upgrade].seconds
                    data['color'] = ''

                    print '    %s (%s)' %(upgrade,
                                          upgrade_times[upgrade].seconds)
                    if upgrade == 'patchset':
                        if upgrade_times[upgrade].seconds > 30:
                            data['color'] = 'bgcolor="#FA5858"'
                            data['result'] = 'Failed: patchset too slow'
                            print '        Failed'

                if final_version:
                    subsubcursor.execute('select max(migration) from '
                                         'patchset_migrations where id="%s" '
                                         'and number=%s;'
                                         %(row['id'], row['number']))
                    subsubrow = subsubcursor.fetchone()
                    data['expected_final_schema_version'] = \
                        subsubrow['max(migration)']
                    if final_version != subsubrow['max(migration)']:
                        data['color'] = 'bgcolor="#FA5858"'
                        data['result'] = 'Failed: incorrect final version'
                        print '        Failed'

                f.write('<ul>%s</ul>' % ('\n'.join(display_upgrades)))
                f.write('<pre><code>\n')
                f.write(''.join(buffered))
                f.write('</code></pre></body></html>')

                with open(datapath, 'w') as d:
                    d.write(json.dumps(data))

    # Write out an index file
    write_index('select * from work_queue order by heartbeat desc limit 100;',
                '/var/www/ci/index.html')
    write_index('select * from work_queue order by heartbeat desc;',
                '/var/www/ci/all.html')

    # Email out results, but only if all tests complete
    candidates = {}
    cursor.execute('select * from work_queue where done is not null and '
                   'emailed is null;')
    for row in cursor:
        candidates[(row['id'], row['number'])] = True

    for ident, number in candidates:
        cursor.execute('select count(*) from work_queue where id="%s" and '
                       'number=%s and done is null;'
                       %(ident, number))
        row = cursor.fetchone()
        if row['count(*)'] > 0:
            print '    %s #%s not complete' %(ident, number)
            continue

        # If we get here, then we owe people an email about a complete run of
        # tests
        results = {}
        cursor.execute('select * from work_queue where id="%s" and number=%s '
                       'and done="y";'
                       %(ident, number))
        for row in cursor:
            results.setdefault(row['workname'], {})
            results[row['workname']].setdefault(row['attempt'], [])
            results[row['workname']][row['attempt']].append(
                          '%s attempt %s:'
                          %(test_name_as_display(row['workname']),
                            row['attempt']))
            try:
                with open('/var/www/ci/%s/%s/%s/data'
                          %(row['id'], row['number'], row['workname'])) as f:
                     data = json.loads(f.read())

                     if data.get('result', ''):
                         results[row['workname']][row['attempt']].append(
                           '    %s' % data.get('result', ''))

                     for upgrade in data['order']:
                         results[row['workname']][row['attempt']].append(
                           '    %s: %s' %(upgrade,
                                          data['details'][upgrade]))
            except Exception, e:
                print 'Error: %s' % e

            url = ('http://openstack.stillhq.com/ci/%s/%s/%s%s/log.html'
                   %(row['id'], row['number'], row['workname'],
                     utils.format_attempt_path(row['attempt'])))
            results[row['workname']][row['attempt']].append(
                          '    Log URL: %s' % url)
            results[row['workname']][row['attempt']].append('')

        result = []
        for workname in sorted(results.keys()):
            attempt = max(results[workname].keys())
            for line in results[workname][attempt]:
                result.append(line)

        print 'Emailing %s #%s' %(row['id'], row['number'])
        utils.send_email('Patchset %s #%s' %(row['id'], row['number']),
                         'ci@lists.stillhq.com',
                         NEW_RESULT_EMAIL
                         % {'results': '\n'.join(result)})

        for workname in results:
            for attempt in results[workname]:
                subcursor.execute('update work_queue set emailed = "y" where '
                                  'id="%s" and number=%s and workname="%s" '
                                  'and attempt=%s;'
                                  %(row['id'], row['number'], workname,
                                    attempt))
        subcursor.execute('commit;')

    # Write a log of all migrations we have ever seen
    cursor.execute('select max(migration) from patchset_migrations;')
    max_migration = cursor.fetchone()['max(migration)']
    for i in range(max_migration - 10, max_migration + 1):
        with open(os.path.join('/var/www/ci/migrations/nova',
                               str(i) + '.html'),
                  'w') as f:
            sql = ('select distinct(id) from patchset_files '
                   'where filename like '
                   '"nova/db/sqlalchemy/migrate_repo/versions/%s_%%" '
                   'order by id;'
                   % i)
            cursor.execute(sql)
            counter = 1
            for row in cursor:
                f.write('<li><a href="http://review.openstack.org/#/q/%s,n,z">'
                        '%s</a>' %(row['id'], row['id']))
                counter += 1
            f.write('<br/><br/>%d patchsets' %(counter - 1))
