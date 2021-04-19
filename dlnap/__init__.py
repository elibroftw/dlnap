# @author cherezov.pavel@gmail.com
# @author elijahllopezz@gmail.com

__version__ = '0.15.0'

import re
import time
import socket
import select
import logging
import traceback
from urllib.request import urlopen
import threading

SSDP_GROUP = ('239.255.255.250', 1900)
URN_AVTransport = 'urn:schemas-upnp-org:service:AVTransport:1'
URN_AVTransport_Fmt = 'urn:schemas-upnp-org:service:AVTransport:{}'
URN_RenderingControl = 'urn:schemas-upnp-org:service:RenderingControl:1'
URN_RenderingControl_Fmt = 'urn:schemas-upnp-org:service:RenderingControl:{}'
SSDP_ALL = 'ssdp:all'


# XML to DICT
def _get_tag_value(x, i=0):
    """ Get the nearest to 'i' position xml tag name.
    x -- xml string
    i -- position to start searching tag from
    return -- (tag, value) pair.
       e.g
          <d>
             <e>value4</e>
          </d>
       result is ('d', '<e>value4</e>')
    """
    x = x.strip()
    value = ''
    tag = ''

    # skip <? > tag
    if x[i:].startswith('<?'):
        i += 2
        while i < len(x) and x[i] != '<':
            i += 1

    # check for empty tag like '</tag>'
    if x[i:].startswith('</'):
        i += 2
        in_attr = False
        while i < len(x) and x[i] != '>':
            if x[i] == ' ':
                in_attr = True
            if not in_attr:
                tag += x[i]
            i += 1
        return tag.strip(), '', x[i + 1:],

    # not an xml, treat like a value
    if not x[i:].startswith('<'):
        return '', x[i:], ''

    i += 1  # <

    # read first open tag
    in_attr = False
    while i < len(x) and x[i] != '>':
        # get rid of attributes
        if x[i] == ' ':
            in_attr = True
        if not in_attr:
            tag += x[i]
        i += 1

    i += 1  # >

    # replace self-closing <tag/> by <tag>None</tag>
    empty_elmt = '<' + tag + ' />'
    closed_elmt = '<' + tag + '>None</' + tag + '>'
    if x.startswith(empty_elmt):
        x = x.replace(empty_elmt, closed_elmt)

    while i < len(x):
        value += x[i]
        if x[i] == '>' and value.endswith('</' + tag + '>'):
            # Note: will not work with xml like <a> <a></a> </a>
            close_tag_len = len(tag) + 2  # />
            value = value[:-close_tag_len]
            break
        i += 1
    return tag.strip(), value[:-1], x[i + 1:]


def _xml2dict(xml_string, ignore_until_xml=False):
    """ Convert xml to dictionary.

    <?xml version="1.0"?>
    <a any_tag="tag value">
       <b> <bb>value1</bb> </b>
       <b> <bb>value2</bb> </b>
       </c>
       <d><e>value4</e></d>
       <g>value</g>
    </a>
    =>
    { 'a': {
          'b': [ {'bb':value1}, {'bb':value2} ],
          'c': [],
          'd': { 'e': [value4] },
          'g': [value]
        } }
    """
    if ignore_until_xml:
        xml_string = ''.join(re.findall(".*?(<.*)", xml_string, re.M))

    _dict = {}
    while xml_string:
        tag, value, xml_string = _get_tag_value(xml_string)
        value = value.strip()
        is_xml, _, _ = _get_tag_value(value)
        if tag not in _dict:
            _dict[tag] = []
        if not is_xml:
            if not value:
                continue
            _dict[tag].append(value.strip())
        else:
            if tag not in _dict:
                _dict[tag] = []
            _dict[tag].append(_xml2dict(value))
    return _dict


def _xpath(xml_dict, path):
    """ Return value from xml dictionary at path.

    d -- xml dictionary
    path -- string path like root/device/serviceList/service@serviceType=URN_AVTransport/controlURL
    return -- value at path or None if path not found
    """

    for p in path.split('/'):
        tag_attr = p.split('@')
        tag = tag_attr[0]
        if tag not in xml_dict:
            return None
        attr = tag_attr[1] if len(tag_attr) > 1 else ''
        if attr:
            a, aval = attr.split('=')
            for s in xml_dict[tag]:
                if s[a] == [aval]:
                    xml_dict = s
                    break
        else:
            xml_dict = xml_dict[tag][0]
    return xml_dict


def _get_port(location):
    """ Extract port number from url.

    location -- string like http://anyurl:port/whatever/path
    return -- port number
    """
    port = re.findall(r'http://.*?:(\d+).*', location)
    return int(port[0]) if port else 80


def _get_control_url(xml, urn):
    """ Extract AVTransport control url from device description xml

    xml -- device description xml
    return -- control url or empty string if wasn't found
    """
    return _xpath(xml, 'root/device/serviceList/service@serviceType={}/controlURL'.format(urn))


def _unescape_xml(xml):
    """ Replace escaped xml symbols with real ones. """
    return xml.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')


# noinspection PyBroadException
def _send_tcp(to, payload):
    """ Send TCP message to group

    to -- (host, port) group to send to payload to
    payload -- message to send
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.settimeout(5)
            sock.connect(to)
            sock.sendall(payload.encode('utf-8'))

            data = sock.recv(2048)
            data = data.decode('utf-8')
            data = _xml2dict(_unescape_xml(data), True)

            error_description = _xpath(data, 's:Envelope/s:Body/s:Fault/detail/UPnPError/errorDescription')
            if error_description is not None:
                logging.error(error_description)
        except Exception:
            data = ''
    return data


def _get_location_url(raw):
    """ Extract device description url from discovery response

    raw -- raw discovery response
    return -- location url string
    """
    t = re.findall(r'\n(?i)location:\s*(.*)\r\s*', raw, re.M)
    if len(t) > 0:
        return t[0]
    return ''


def _get_serve_ip(target_ip, target_port=80):
    """ Find ip address of network interface used to communicate with target

    target-ip -- ip address of target
    return -- ip address of interface connected to target
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((target_ip, target_port))
    my_ip = s.getsockname()[0]
    s.close()
    return my_ip


# noinspection PyBroadException
class DLNADevice:
    """ Represents DLNA device."""

    def __init__(self, raw, ip):
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.__logger.info('=> new DLNADevice (ip = {}) initialization..'.format(ip))

        self.ip = ip
        self.ssdp_version = 1
        self.port = None
        self.name = 'Unknown'
        self.control_url = None
        self.rendering_control_url = None
        self.has_av_transport = False

        try:
            self.__raw = raw.decode()
            self.location = _get_location_url(self.__raw)
            self.__logger.info('location: {}'.format(self.location))

            self.port = _get_port(self.location)
            self.__logger.info(f'port: {self.port}')

            raw_desc_xml = urlopen(self.location).read().decode()

            self.__desc_xml = _xml2dict(raw_desc_xml)
            self.__logger.debug(f'description xml: {self.__desc_xml}')

            self.name = self._get_friendly_name(self.__desc_xml)
            self.__logger.info(f'friendlyName: {self.name}')

            self.control_url = _get_control_url(self.__desc_xml, URN_AVTransport)
            self.__logger.info(f'control_url: {self.control_url}')

            self.rendering_control_url = _get_control_url(self.__desc_xml, URN_RenderingControl)
            self.__logger.info(f'rendering_control_url: {self.rendering_control_url}')

            self.has_av_transport = self.control_url is not None
            self.__logger.info('=> Initialization completed'.format(ip))
        except Exception:
            self.__logger.warning(f'DLNADevice (ip = {ip}) init exception:\n{traceback.format_exc()}')

    def __repr__(self):
        return f'DLNADevice({self.name} @ {self.ip})'

    def __eq__(self, d):
        return self.name == d.name and self.ip == d.ip

    @staticmethod
    def _payload_from_template(action, data, urn):
        """ Assembly payload from template.
        """
        fields = ''
        for tag, value in data.items():
            fields += '<{tag}>{value}</{tag}>'.format(tag=tag, value=value)
        encoding_style = 'http://schemas.xmlsoap.org/soap/encoding/'
        payload = f"""<?xml version="1.0" encoding="utf-8"?>
         <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="{encoding_style}">
            <s:Body>
               <u:{action} xmlns:u="{urn}">
                  {fields}
               </u:{action}>
            </s:Body>
         </s:Envelope>"""
        return payload

    @staticmethod
    def _get_friendly_name(xml):
        """ Extract device name from description xml

        xml -- device description xml
        return -- device name
        """
        name = _xpath(xml, 'root/device/friendlyName')
        return name if name is not None else 'Unknown'

    def _create_packet(self, action, data):
        """ Create packet to send to device control url.

        action -- control action
        data -- dictionary with XML fields value
        """
        if action in ["SetVolume", "SetMute", "GetVolume"]:
            url = self.rendering_control_url
            urn = URN_RenderingControl_Fmt.format(self.ssdp_version)
        else:
            url = self.control_url
            urn = URN_AVTransport_Fmt.format(self.ssdp_version)
        payload = self._payload_from_template(action=action, data=data, urn=urn)

        packet = "\r\n".join([
            'POST {} HTTP/1.1'.format(url),
            'User-Agent: {}/{}'.format(__file__, __version__),
            'Accept: */*',
            'Content-Type: text/xml; charset="utf-8"',
            'HOST: {}:{}'.format(self.ip, self.port),
            'Content-Length: {}'.format(len(payload)),
            'SOAPACTION: "{}#{}"'.format(urn, action),
            'Connection: close',
            '',
            payload,
        ])

        self.__logger.debug(packet)
        return packet

    def play_media(self, url, instance_id=0, autoplay=True):
        """ Set media to playback and play if autoplay
        url -- media url
        instance_id -- device instance id
        """
        # TODO: test if need to stop() first
        packet = self._create_packet('SetAVTransportURI',
                                     {'InstanceID': instance_id, 'CurrentURI': url, 'CurrentURIMetaData': ''})
        _send_tcp((self.ip, self.port), packet)
        if autoplay: self.resume()

    def resume(self, instance_id=0):
        """ Resume (or play) media that has already been set as current.

        instance_id -- device instance id
        """
        packet = self._create_packet('Play', {'InstanceID': instance_id, 'Speed': 1})
        _send_tcp((self.ip, self.port), packet)

    def pause(self, instance_id=0):
        """ Pause media that is currently playing back.

        instance_id -- device instance id
        """
        packet = self._create_packet('Pause', {'InstanceID': instance_id, 'Speed': 1})
        _send_tcp((self.ip, self.port), packet)

    def stop(self, instance_id=0):
        """ Stop media that is currently playing back.
        instance_id -- device instance id
        """
        packet = self._create_packet('Stop', {'InstanceID': instance_id, 'Speed': 1})
        _send_tcp((self.ip, self.port), packet)

    def seek(self, position: int, instance_id=0):
        """
        position: position to seek to in seconds
        """
        position = time.strftime('%H:%M:%S', time.gmtime(position))
        packet = self._create_packet('Seek', {'InstanceID': instance_id, 'Unit': 'REL_TIME', 'Target': position})
        _send_tcp((self.ip, self.port), packet)

    def set_volume(self, volume, instance_id=0):
        """ set volume
        Volume from [0, 100]
        instance_id -- device instance id
        """
        packet = self._create_packet('SetVolume',
                                     {'InstanceID': instance_id, 'DesiredVolume': volume, 'Channel': 'Master'})

        _send_tcp((self.ip, self.port), packet)

    def get_volume(self, instance_id=0):
        """ get volume """
        packet = self._create_packet('GetVolume', {'InstanceID': instance_id, 'Channel': 'Master'})
        _send_tcp((self.ip, self.port), packet)

    def mute(self, instance_id=0):
        """ mute volume
        instance_id -- device instance id
        """
        packet = self._create_packet('SetMute', {'InstanceID': instance_id, 'DesiredMute': '1', 'Channel': 'Master'})
        _send_tcp((self.ip, self.port), packet)

    def unmute(self, instance_id=0):
        """ unmute volume
        instance_id -- device instance id
        """
        packet = self._create_packet('SetMute', {'InstanceID': instance_id, 'DesiredMute': '0', 'Channel': 'Master'})
        _send_tcp((self.ip, self.port), packet)

    def info(self, instance_id=0):
        """ Transport info.
        instance_id -- device instance id
        """
        packet = self._create_packet('GetTransportInfo', {'InstanceID': instance_id})
        return _send_tcp((self.ip, self.port), packet)

    def media_info(self, instance_id=0):
        """ Media info.
        instance_id -- device instance id
        """
        packet = self._create_packet('GetMediaInfo', {'InstanceID': instance_id})
        return _send_tcp((self.ip, self.port), packet)

    def position_info(self, instance_id=0):
        """ Position info.
        instance_id -- device instance id
        """
        packet = self._create_packet('GetPositionInfo', {'InstanceID': instance_id})
        return _send_tcp((self.ip, self.port), packet)


def get_dlnas(scan_for=2, st=URN_AVTransport_Fmt, mx=3, ssdp_version=1, blocking=True, callback=None):
    """ Discover UPnP devices in the local network.
    timeout -- timeout to perform get_dlnas
    st -- st field of discovery packet
    mx -- mx field of discovery packet
    return -- list of DLNAUPnPDevice (blocking=True)
    return -- function to stop discovery (blocking=False)
    """
    st = st.format(ssdp_version)
    payload = "\r\n".join([
        'M-SEARCH * HTTP/1.1',
        'User-Agent: {}/{}'.format(__file__, __version__),
        'HOST: {}:{}'.format(*SSDP_GROUP),
        'Accept: */*',
        'MAN: "ssdp:discover"',
        'ST: {}'.format(st),
        'MX: {}'.format(mx),
        '',
        ''])
    keep_scanning = False

    def _stop_discovery():
        nonlocal keep_scanning
        keep_scanning = False

    def _discovery():
        nonlocal scan_for
        devices = set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.sendto(payload.encode(), SSDP_GROUP)
            scan_for = time.monotonic() + scan_for
            while keep_scanning and time.monotonic() < scan_for:
                r, w, x = select.select([sock], [], [sock], 1)
                if sock in r:
                    data, addr = sock.recvfrom(1024)
                    device = DLNADevice(data, addr[0])
                    device.ssdp_version = ssdp_version
                    if device not in devices:
                        if callable(callback): callback(device)
                        devices.add(device)
                elif sock in x:
                    raise Exception('Getting response failed')
        return devices

    if blocking:
        return _discovery()
    threading.Thread(name='DLNA discovery', target=_discovery).start()
    return _stop_discovery
