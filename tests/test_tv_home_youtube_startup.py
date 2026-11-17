"""The injected YouTube startup handler recovers a missed preloaded launch."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_startup_recovery():
    script = Path(__file__).parent / '../tv/youtube-startup-recovery.js'
    harness = r'''
const assert = require('assert'), vm = require('vm'), fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
function run(params, href, previous) {
  const timers = [], values = previous || {};
  const window = {launchParams: JSON.stringify(params), location: {href},
    sessionStorage: {getItem: k => values[k] || null, setItem: (k, v) => values[k] = v}};
  vm.runInNewContext(source, {window, URL, setTimeout: f => timers.push(f), Date});
  timers.forEach(f => f());
  return window.location.href;
}
const home = 'https://www.youtube.com/tv#/', id = 'Oqy5TXXYR2c';
const params = {firstLaunchSincePreload: true, target: 'v=' + id};
assert.match(run(params, home), /[?&]v=Oqy5TXXYR2c/);
const watch = home + 'watch?v=' + id;
assert.equal(run(params, watch), watch);
assert.equal(run({firstLaunchSincePreload: true}, home), home);
assert.equal(run({target: 'v=' + id}, home), home);
assert.equal(run({firstLaunchSincePreload: true, target: 'v=bad'}, home), home);
const previous = {'tvhome-youtube-startup-recovery': JSON.stringify({id, at: Date.now()})};
assert.equal(run(params, home, previous), home);
'''
    node = shutil.which('node')   # tools/check.sh puts mise's shims on PATH
    if not node:
        pytest.skip('node not installed')
    subprocess.run([node, '-e', harness, str(script)], check=True)
