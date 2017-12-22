#!/usr/bin/env python
import os
import re
import sys
import json
import ltsv
import requests
import numpy as np
from time import mktime
from collections import defaultdict
from datetime import datetime, timedelta
from pyzabbix import ZabbixSender, ZabbixMetric

if sys.version_info[0] == 3:
    from io import StringIO
else:
    from cStringIO import StringIO

ZABBIX_AGENT_CONFIG = '/etc/zabbix/zabbix_agentd.conf'
PERCENTILES = (50, 75, 90)


def calc_percentile(data, value):
    return round(np.percentile(data, value), 3)

def is_error_code(code):
    return code in ('404', '403', '409', '400', '499', '503', '500', '412',)

class LogParser:
    """
    working with log and position files
    collects the statistic by minutes and sends it to zabbix server
    using
    """

    def __init__(self, filename, pos_file):
        self.log_file = filename
        self.pos_file = pos_file
        self.host = os.uname()[1]
        self.elastic_metric = defaultdict(
            lambda: {
                'index': {'count': 0, 'errors': 0, 'req_times': [], 'up_times': []},
                'refresh': {'count': 0, 'errors': 0, 'req_times': [], 'up_times': []}
            }
        )
        self.run()

    def run(self):
        pos, line = self.get_start_pos_n_line()
        self.read_nginx_log(pos, line)

    def get_start_pos_n_line(self):
        """
        checks pos_file existence
        read values
        :return: (int start_position, str last_line)
        """
        if not os.path.isfile(self.pos_file):
            return 0, ''
        with open(self.pos_file) as f:
            try:
                pos = int(f.readline().strip())
            except ValueError:
                pos = 0
            line = f.readline().strip()
            return pos, line

    def send_data(self, timestamp):
        packet = []
        key_prefix = 'ESZabbix_logs'
        for index, data in self.elastic_metric.items():
            not_empty = True if len(data['index']['req_times']) > 0 else False
            for p in PERCENTILES:
                data['index']['percentile_%s' % p] = calc_percentile(data['index']['req_times'], p) if not_empty else 0

            for k in ('count', 'errors', 'percentile_50', 'percentile_75', 'percentile_90'):
                key_template = '{key}[bulk,{metric_name}]' if index == 'bulk' else '{key}[{index},index,{metric_name}]'
                packet.append(ZabbixMetric(
                    host=self.host,
                    key=key_template.format(index=index, key=key_prefix, metric_name=k),
                    value=data['index'][k],
                    clock=timestamp
                ))
            if index != 'bulk':
                not_empty = True if len(data['refresh']['req_times']) > 0 else False
                for p in PERCENTILES:
                    data['refresh']['percentile_%s' % p] = calc_percentile(
                        data['refresh']['req_times'], p
                    ) if not_empty else 0

                for k in ('count', 'errors', 'percentile_50', 'percentile_75', 'percentile_90'):
                    packet.append(ZabbixMetric(
                        host=self.host,
                        key='{key}[{index},refresh,{metric_name}]'.format(index=index, key=key_prefix, metric_name=k),
                        value=data['refresh'][k],
                        clock=timestamp
                    ))

        self.elastic_metric.clear()
        for el in packet:
            print(el)
        print(ZabbixSender(use_config=ZABBIX_AGENT_CONFIG).send(packet))

    def read_nginx_log(self, start_pos, last_line):
        """
        checks that file is valid and get new data
        :param start_pos: int, start position in log file
        :param last_line: str, last line value
        :return: None
        elastic_metric = {
            'url': {
                'index': {
                    'count': 0,
                    'errors': 0,
                    'req_time': [],
                    'up_time': [],
                },
                'refresh': {
                    'count': 0,
                    'errors': 0,
                    'req_time': [],
                    'up_time': [],
                }
            },
        }
        """
        if not os.path.isfile(self.log_file):
            return
        with open(self.log_file, 'r') as log_file:
            log_file.seek(start_pos, 0)
            if log_file.readline().strip() != last_line:
                print('starting from the beginning')
                log_file.seek(0, 0)

            """
            python repesentation of ltsv
            [['host', '127.0.0.1'], ['user', '-'], ['time', '[22/Sep/2017:14:36:39 +0000]'],
            ['request', 'GET / HTTP/1.1'], ['status', '200'], ['size', '323'], ['referer', '-'],
            ['user_agent', 'curl/7.47.0'], ['req_time', '0.060'],
            ['upstream_res_time', '0.060'], ['upstream_addr', '127.0.0.1:9200']]
            """
            start_parsing_time = datetime.now()
            last_tell = start_pos
            cur_tell = log_file.tell()
            line = log_file.readline()
            if line:
                start_params = next(ltsv.reader(StringIO(line)))
                start_time = datetime.strptime(start_params[2][1].split(' ')[0], '[%d/%b/%Y:%H:%M:%S')
            else:
                self.write_cur_pos_n_line(last_tell, last_line)
                return last_tell, last_line
            while line:
                last_line = line
                last_tell = cur_tell
                line_params = next(ltsv.reader(StringIO(line)))
                line_time = datetime.strptime(line_params[2][1].split(' ')[0], '[%d/%b/%Y:%H:%M:%S')
                if line_time >= start_parsing_time:
                    timestamp = int(mktime(start_time.timetuple()))
                    self.send_data(timestamp)
                    self.write_cur_pos_n_line(cur_tell, line)
                    return cur_tell, line
                if line_time - start_time > timedelta(minutes=1):
                    timestamp = int(mktime(start_time.timetuple()))
                    self.send_data(timestamp)
                    start_time = line_time
                url = line_params[3][1].split()[1]
                req_times = map(float,line_params[8][1].split(','))
                up_times = map(float,line_params[9][1].split(','))
                if re.match('/_bulk', url):
                    self.elastic_metric['bulk']['index']['count'] += 1
                    for req_time in req_times:
                        self.elastic_metric['bulk']['index']['req_times'].append(req_time)
                    for up_time in up_times:
                        self.elastic_metric['bulk']['index']['up_times'].append(up_time)
                elif re.match('^(/[^_]\w+[?/]+)', url):
                    index = re.findall('^/([^_]\w+)[/?]', url)[0]
                    if not index:
                        continue
                    self.elastic_metric[index]['index']['count'] += 1
                    for req_time in req_times:
                        self.elastic_metric[index]['index']['req_times'].append(req_time)
                    for up_time in up_times:
                        self.elastic_metric[index]['index']['req_times'].append(up_time)

                    if is_error_code(line_params[4][1]):
                        self.elastic_metric[index]['index']['errors'] += 1
                    if re.match('^(/[^_]?\w+/_refresh)', url):
                        self.elastic_metric[index]['refresh']['count'] += 1
                        for req_time in req_times:
                            self.elastic_metric[index]['refresh']['req_times'].append(req_time)
                        for up_time in up_times:
                            self.elastic_metric[index]['refresh']['up_times'].append(up_time)
                        if is_error_code(line_params[4][1]):
                            self.elastic_metric[index]['refresh']['errors'] += 1
                cur_tell = log_file.tell()
                line = log_file.readline()
            timestamp = int(mktime(start_time.timetuple()))
            self.send_data(timestamp)
            self.write_cur_pos_n_line(last_tell, last_line)
        return last_tell, last_line


    def write_cur_pos_n_line(self, pos, line):
        with open(self.pos_file, 'w') as f:
            f.write('{}\n{}\n'.format(str(pos), line))


if __name__ == '__main__':
    if sys.argv[1] == 'elasticsearch.discovery':
        r = requests.get('http://localhost:9200/_cat/indices?v')
        strings = r.text.strip().split('\n')
        res_data = {'data': []}
        for string in strings[1:]:
            res_data['data'].append({'{#ES_INDEX}': '{}'.format(string.split()[2])})
        print(json.dumps(res_data))
    else:
        LogParser(sys.argv[1], '/tmp/%s.es.position' % re.sub('_+', '_', re.sub('[^a-z0-9A-Z]+', '_', sys.argv[1])))
