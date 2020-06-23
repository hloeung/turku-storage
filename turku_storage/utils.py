# Turku backups - storage module
# Copyright 2015 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
# SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.

import copy
import datetime
import glob
import json
import os
import pwd
import random
import re
import socket
import time
import urllib.parse

import requests


class RuntimeLock:
    name = None
    file = None

    def __init__(self, name):
        import fcntl

        file = open(name, "w")
        try:
            fcntl.lockf(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError as e:
            import errno

            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise
        file.write("%10s\n" % os.getpid())
        file.flush()
        file.seek(0)
        self.name = name
        self.file = file

    def close(self):
        if self.file:
            self.file.close()
            self.file = None
            os.unlink(self.name)

    def __del__(self):
        self.close()

    def __enter__(self):
        self.file.__enter__()
        return self

    def __exit__(self, exc, value, tb):
        result = self.file.__exit__(exc, value, tb)
        self.close()
        return result


def acquire_lock(name):
    return RuntimeLock(name)


def json_dump_p(obj, f):
    """Calls json.dump with standard (pretty) formatting"""
    return json.dump(obj, f, sort_keys=True, indent=4, separators=(",", ": "))


def json_dumps_p(obj):
    """Calls json.dumps with standard (pretty) formatting"""
    return json.dumps(obj, sort_keys=True, indent=4, separators=(",", ": "))


def json_load_file(file):
    with open(file) as f:
        try:
            return json.load(f)
        except ValueError as e:
            e.args += (file,)
            raise


def dict_merge(s, m):
    """Recursively merge one dict into another."""
    if not isinstance(m, dict):
        return m
    out = copy.deepcopy(s)
    for k, v in list(m.items()):
        if k in out and isinstance(out[k], dict):
            out[k] = dict_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def api_call(api_url, cmd, post_data, timeout=5):
    """Turku API call client"""
    url = urllib.parse.urljoin(api_url + "/", cmd)
    headers = {"Accept": "application/json"}
    r = requests.post(url, json=post_data, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def random_weighted(m):
    """Return a weighted random key."""
    total = sum(list(m.values()))
    if total <= 0:
        return random.choice(list(m.keys()))
    weighted = []
    tp = 0
    for k, v in list(m.items()):
        tp = tp + (float(v) / float(total))
        weighted.append((k, tp))
    r = random.random()
    for (k, v) in weighted:
        if r < v:
            return k


def load_config(config_dir):
    config = {}
    config_d = os.path.join(config_dir, "config.d")
    config_files = [
        os.path.join(config_d, fn)
        for fn in os.listdir(config_d)
        if fn.endswith(".json")
        and os.path.isfile(os.path.join(config_d, fn))
        and os.access(os.path.join(config_d, fn), os.R_OK)
    ]
    config_files.sort()
    for file in config_files:
        config = dict_merge(config, json_load_file(file))

    required_keys = ["name", "secret", "api_url", "volumes"]
    # XXX legacy
    if "api_auth" not in config:
        required_keys += ["api_auth_name", "api_auth_secret"]
    for k in required_keys:
        if k not in config:
            raise Exception("Incomplete config")

    if "accept_new_high_water_pct" not in config:
        config["accept_new_high_water_pct"] = 80

    for volume_name in config["volumes"]:
        if "path" not in config["volumes"][volume_name]:
            del config["volumes"][volume_name]
            continue
        if "accept_new" not in config["volumes"][volume_name]:
            config["volumes"][volume_name]["accept_new"] = True
        if "accept_new_high_water_pct" not in config["volumes"][volume_name]:
            config["volumes"][volume_name]["accept_new_high_water_pct"] = config[
                "accept_new_high_water_pct"
            ]

    if len(config["volumes"]) == 0:
        raise Exception("Incomplete config")

    if "log_file" not in config:
        config["log_file"] = "/var/log/turku-storage.log"
    if "lock_dir" not in config:
        config["lock_dir"] = "/var/lock"
    if "var_dir" not in config:
        config["var_dir"] = "/var/lib/turku-storage"

    if "snapshot_mode" not in config:
        config["snapshot_mode"] = "link-dest"
    if "preserve_hard_links" not in config:
        config["preserve_hard_links"] = False

    if "ssh_ping_host" not in config:
        config["ssh_ping_host"] = socket.getfqdn()
    if "ssh_ping_port" not in config:
        config["ssh_ping_port"] = 22
    if "ssh_ping_user" not in config:
        config["ssh_ping_user"] = "root"
    if "ssh_ping_host_keys" not in config:
        config["ssh_ping_host_keys"] = []
        keys_glob = "/etc/ssh/ssh_host_*_key.pub"
        if "ssh_ping_host_keys_glob" in config:
            keys_glob = config["ssh_ping_host_keys_glob"]
        for pubkey in glob.glob(keys_glob):
            with open(pubkey) as f:
                config["ssh_ping_host_keys"].append(f.read().rstrip())
    if "authorized_keys_file" not in config:
        config["authorized_keys_file"] = (
            "%s/.ssh/authorized_keys" % pwd.getpwnam(config["ssh_ping_user"]).pw_dir
        )
    if "authorized_keys_user" not in config:
        config["authorized_keys_user"] = config["ssh_ping_user"]
    if "authorized_keys_command" not in config:
        config["authorized_keys_command"] = "turku-storage-ping"

    if "timezone" not in config:
        config["timezone"] = "UTC"
    if config["timezone"]:
        os.environ["TZ"] = config["timezone"]
    time.tzset()

    return config


def parse_snapshot_name(ss):
    # If a snapshot name matches one of these formats
    #     1424392089.43
    #     2015-02-20T03:20:36
    #     2015-02-20T03:21:18.152575
    # use it as a timestamp, otherwise ignore it
    if "save" in ss:
        raise ValueError("Excluded snapshot")
    if ss == "working":
        raise ValueError("Excluded snapshot")
    try:
        return datetime.datetime.strptime(ss, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(ss, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.datetime.utcfromtimestamp(float(ss))
    except ValueError:
        pass
    raise ValueError("Unknown snapshot name format")


def get_latest_snapshot(snapshots):
    snapshot_dict = {}
    for ss in snapshots:
        try:
            snapshot_dict[parse_snapshot_name(ss)] = ss
        except ValueError:
            pass
    if len(snapshot_dict) == 0:
        return None
    return snapshot_dict[max(list(snapshot_dict.keys()))]


def get_snapshots_to_delete(retention, snapshots):
    snapshot_dict = {}
    for ss in snapshots:
        try:
            snapshot_dict[parse_snapshot_name(ss)] = ss
        except ValueError:
            pass

    now = datetime.datetime.now()
    to_keep = []
    for ritem in retention.split(","):
        ritem = ritem.strip()
        r = re.findall(r"^earliest of (?:(\d+) )?(day|week|month)", ritem)
        if len(r) > 0:
            if r[0][0] == "":
                earliest_num = 1
            else:
                earliest_num = int(r[0][0])
            earliest_word = r[0][1]
            if earliest_word == "fortnight":
                earliest_word = "week"
                earliest_num = earliest_num * 2
            if earliest_word == "day":
                cutoff_time = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - datetime.timedelta(days=(earliest_num - 1))
            elif earliest_word == "week":
                cutoff_time = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - datetime.timedelta(days=((now.weekday() + 1) % 7))
                for i in range(earliest_num - 1):
                    cutoff_time = (cutoff_time - datetime.timedelta(weeks=1)).replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    )
            elif earliest_word == "month":
                cutoff_time = now.replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                )
                for i in range(earliest_num - 1):
                    cutoff_time = (cutoff_time - datetime.timedelta(days=1)).replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    )
            else:
                cutoff_time = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - datetime.timedelta(days=(earliest_num - 1))
            candidate_s = None
            for s in list(snapshot_dict.keys()):
                if s < cutoff_time:
                    continue
                if not candidate_s:
                    candidate_s = s
                    continue
                if s >= candidate_s:
                    continue
                candidate_s = s
            if candidate_s and candidate_s not in to_keep:
                to_keep.append(candidate_s)
        r = re.findall(r"^last (\d+) day", ritem)
        if len(r) > 0:
            last_days = int(r[0])
            cutoff_time = now - datetime.timedelta(days=last_days)
            for s in list(snapshot_dict.keys()):
                if s < cutoff_time:
                    continue
                if s not in to_keep:
                    to_keep.append(s)
        r = re.findall(r"^last (\d+) snapshot", ritem)
        if len(r) > 0:
            last_snapshots = int(r[0])
            i = 0
            for s in sorted(list(snapshot_dict.keys()), reverse=True):
                i = i + 1
                if s not in to_keep:
                    to_keep.append(s)
                if i == last_snapshots:
                    break

    # If something went wrong and nothing was found to keep,
    # don't delete everything.
    if len(to_keep) == 0:
        return []

    to_delete = []
    for s in list(snapshot_dict.keys()):
        if s not in to_keep:
            to_delete.append(snapshot_dict[s])
    return to_delete
