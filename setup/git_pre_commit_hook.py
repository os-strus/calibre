#!/usr/bin/env python
# License: GPLv3 Copyright: 2008, Kovid Goyal <kovid at kovidgoyal.net>


import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from lxml import html

'''
Hook to make the commit command automatically close bugs when the commit
message contains `Fix #number` or `Implement #number`. Also updates the commit
message with the summary of the closed bug.

'''


LAUNCHPAD = os.path.expanduser('~/work/env/launchpad.py')
LAUNCHPAD_BUG = 'https://bugs.launchpad.net/calibre/+bug/{}'
GITHUB_BUG = 'https://api.github.com/repos/kovidgoyal/calibre/issues/{}'
BUG_PAT = r'(Fix|Implement|Fixes|Fixed|Implemented|See)\s+#(\d+)'

socket.setdefaulttimeout(90)


class Bug:

    def __init__(self):
        self.seen = set()

    def __call__(self, match):
        action, bug = match.group(1), match.group(2)
        summary = ''
        if bug in self.seen:
            return match.group()
        self.seen.add(bug)

        if int(bug) > 100000:  # Launchpad bug
            try:
                raw = urllib.request.urlopen(LAUNCHPAD_BUG.format(bug)).read()
                h1 = html.fromstring(raw).xpath('//h1[@id="edit-title"]')[0]
                summary = html.tostring(h1, method='text', encoding=str).strip()
            except Exception:
                summary = 'Private bug'
        else:
            summary = json.loads(urllib.request.urlopen(GITHUB_BUG.format(bug)).read())['title']
        if summary:
            print('Working on bug:', summary)
            if int(bug) > 100000 and action != 'See':
                self.close_bug(bug, action)
                return match.group() + f' [{summary}]({LAUNCHPAD_BUG.format(bug)})'
            return match.group() + f' ({summary})'
        return match.group()

    def close_bug(self, bug, action):
        print(f'Closing bug #{bug}')
        suffix = (
            'The fix will be in the next release. '
            'calibre is usually released every alternate Friday.'
        )
        action += 'ed'
        msg = '{} in branch {}. {}'.format(action, 'master', suffix)
        msg = msg.replace('Fixesed', 'Fixed')
        env = dict(os.environ)
        env['LAUNCHPAD_FIX_BUG'] = msg
        subprocess.run([sys.executable, LAUNCHPAD], env=env, input=f'Subject: [Bug {bug}]', text=True, check=True)


def main():
    with open(sys.argv[-1], 'r+b') as f:
        raw = f.read().decode('utf-8')
        bug = Bug()
        msg = re.sub(BUG_PAT, bug, raw)
        if msg != raw:
            f.seek(0)
            f.truncate()
            f.write(msg.encode('utf-8'))


if __name__ == '__main__':
    main()
