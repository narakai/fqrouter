import logging
import logging.handlers
import sys
import os
import _multiprocessing
import socket
import httplib
import fqdns
import fqsocks.fqsocks
import contextlib

import gevent
import gevent.monkey
import gevent.socket
import dpkt

from utils import httpd


LOGGER = logging.getLogger('fqrouter.%s' % __name__)
LOG_DIR = '/data/data/fq.router'
MANAGER_LOG_FILE = os.path.join(LOG_DIR, 'manager.log')
FQDNS_LOG_FILE = os.path.join(LOG_DIR, 'fqdns.log')

nat_map = {} # sport => (dst, dport), src always be 10.25.1.1
DNS_HANDLER = fqdns.DnsHandler(enable_china_domain=True, enable_hosted_domain=True)


def redirect_tun_traffic(tun_fd):
    while True:
        try:
            redirect_ip_packet(tun_fd)
        except:
            LOGGER.exception('failed to handle ip packet')


def redirect_ip_packet(tun_fd):
    gevent.socket.wait_read(tun_fd)
    ip_packet = dpkt.ip.IP(os.read(tun_fd, 8192))
    src = socket.inet_ntoa(ip_packet.src)
    dst = socket.inet_ntoa(ip_packet.dst)
    if hasattr(ip_packet, 'udp'):
        l4_packet = ip_packet.udp
    elif hasattr(ip_packet, 'tcp'):
        l4_packet = ip_packet.tcp
    else:
        return
    if src != '10.25.1.1':
        return
    if dst == '10.25.1.100':
        orig_dst_addr = nat_map.get(l4_packet.dport)
        if not orig_dst_addr:
            raise Exception('failed to get original destination')
        orig_dst, orig_dport = orig_dst_addr
        ip_packet.src = socket.inet_aton(orig_dst)
        ip_packet.dst = socket.inet_aton('10.25.1.1')
        ip_packet.sum = 0
        l4_packet.sport = orig_dport
        l4_packet.sum = 0
    else:
        nat_map[l4_packet.sport] = (dst, l4_packet.dport)
        ip_packet.src = socket.inet_aton('10.25.1.100')
        ip_packet.dst = socket.inet_aton('10.25.1.1')
        ip_packet.sum = 0
        l4_packet.dport = 12345
        l4_packet.sum = 0
    gevent.socket.wait_write(tun_fd)
    os.write(tun_fd, str(ip_packet))


def serve_udp():
    address = ('10.25.1.1', 12345)
    server = fqdns.HandlerDatagramServer(address, handle_udp)
    LOGGER.info('udp server started at %r', address)
    try:
        server.serve_forever()
    except:
        LOGGER.exception('udp server failed')
    finally:
        LOGGER.info('udp server stopped')


def handle_udp(sendto, request, address):
    try:
        src_ip, src_port = address
        dst_ip, dst_port = nat_map.get(src_port)
        if 53 == dst_port:
            LOGGER.info('pass to DNS_HANDLER')
            DNS_HANDLER(sendto, request, address)
        else:
            sock = create_udp_socket()
            try:
                sock.sendto(request, (dst_ip, dst_port))
                response = sock.recv(8192)
                sendto(response, address)
            finally:
                sock.close()
    except:
        LOGGER.exception('failed to handle udp')


def get_original_destination(sock, src_ip, src_port):
    if src_ip != '10.25.1.100': # fake connection from 10.25.1.100
        raise Exception('unexpected src ip: %s' % src_ip)
    return nat_map.get(src_port)


fqsocks.fqsocks.SPI['get_original_destination'] = get_original_destination


def create_tcp_socket(server_ip, server_port, connect_timeout):
    fdsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with contextlib.closing(fdsock):
        fdsock.connect('\0fdsock')
        fdsock.sendall('TCP,%s,%s,%s\n' % (server_ip, server_port, connect_timeout * 1000))
        gevent.socket.wait_read(fdsock.fileno())
        fd = _multiprocessing.recvfd(fdsock.fileno())
        sock = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((server_ip, server_port))
        return sock


fqdns.SPI['create_tcp_socket'] = create_tcp_socket
fqsocks.fqsocks.SPI['create_tcp_socket'] = create_tcp_socket


def create_udp_socket():
    fdsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with contextlib.closing(fdsock):
        fdsock.connect('\0fdsock')
        fdsock.sendall('UDP\n')
        gevent.socket.wait_read(fdsock.fileno())
        fd = _multiprocessing.recvfd(fdsock.fileno())
        return socket.fromfd(fd, socket.AF_INET, socket.SOCK_DGRAM)


fqdns.SPI['create_udp_socket'] = create_udp_socket
fqsocks.fqsocks.SPI['create_udp_socket'] = create_udp_socket


def setup_logging():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    handler = logging.handlers.RotatingFileHandler(
        MANAGER_LOG_FILE, maxBytes=1024 * 256, backupCount=0)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger('fqrouter').addHandler(handler)
    handler = logging.handlers.RotatingFileHandler(
        FQDNS_LOG_FILE, maxBytes=1024 * 256, backupCount=0)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger('fqdns').addHandler(handler)


def handle_ping(environ, start_response):
    start_response(httplib.OK, [('Content-Type', 'text/plain')])
    LOGGER.info('VPN PONG')
    yield 'VPN PONG'


def read_tun_fd():
    LOGGER.info('connecting to fdsock')
    fdsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with contextlib.closing(fdsock):
        while True:
            try:
                fdsock.connect('\0fdsock')
                break
            except:
                LOGGER.info('retry in 3 seconds')
                gevent.sleep(3)
        LOGGER.info('connected to fdsock')
        fdsock.sendall('TUN\n')
        gevent.socket.wait_read(fdsock.fileno())
        return _multiprocessing.recvfd(fdsock.fileno())


if '__main__' == __name__:
    LOGGER.info('environment: %s' % os.environ.items())
    gevent.monkey.patch_all()
    setup_logging()
    httpd.HANDLERS[('GET', 'ping')] = handle_ping
    greenlets = [gevent.spawn(httpd.serve_forever)]
    try:
        tun_fd = read_tun_fd()
        LOGGER.info('tun fd: %s' % tun_fd)
    except:
        LOGGER.exception('failed to get tun fd')
    greenlets.append(gevent.spawn(redirect_tun_traffic, tun_fd))
    greenlets.append(gevent.spawn(serve_udp))
    greenlets.append(gevent.spawn(fqsocks.fqsocks.main, [
        '--log-level', 'INFO',
        '--log-file', '/data/data/fq.router/fqsocks.log',
        '--listen', '10.25.1.1:12345',
        '--proxy', 'dynamic,n=20,dns_record=proxy#n#.fqrouter.com',
        '--proxy', 'dynamic,n=10,type=goagent,dns_record=goagent#n#.fqrouter.com',
        '--google-host', 'goagent-google-ip.fqrouter.com'
    ]))
    for greenlet in greenlets:
        greenlet.join()