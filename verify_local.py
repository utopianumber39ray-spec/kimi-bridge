#!/usr/bin/env python3
"""Check the actual endpoint, browser CDP, and NVIDIA page without restarting."""
import json
import os
import sys
import urllib.error
import urllib.request

CDP = os.environ.get('KIMI_CDP', 'http://127.0.0.1:9333')
URL = os.environ.get('KIMI_HEALTH_URL', 'http://127.0.0.1:8120/v1/health')


def main():
    with urllib.request.urlopen(CDP + '/json/list', timeout=5) as r:
        tabs = json.load(r)
    pages = [t.get('url', '') for t in tabs if t.get('type') == 'page']
    page = any('/moonshotai/kimi-k3_1-pro/playground' in p for p in pages)
    with urllib.request.urlopen(URL, timeout=6) as r:
        health = json.load(r)
    print('bridge:', 'OK' if health.get('ok') else 'FAIL')
    print('browser:', 'OK — existing Playground page found' if page else 'FAIL: page not found')
    print('CDP:', CDP)
    return 0 if health.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
