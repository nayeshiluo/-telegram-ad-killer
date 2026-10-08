"""Prefer stable IPv4 without changing global sockets or TLS verification."""
import http.client
import socket
import time
import urllib.request

def connect(address,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,source_address=None):
    host,port=address
    candidates=socket.getaddrinfo(host,port,0,socket.SOCK_STREAM)
    candidates.sort(key=lambda item:item[0]!=socket.AF_INET)
    deadline=time.monotonic()+timeout if isinstance(timeout,(int,float)) else None
    last=None
    for family,kind,protocol,_,sockaddr in candidates:
        remaining=deadline-time.monotonic() if deadline is not None else timeout
        if deadline is not None and remaining<=0:break
        sock=socket.socket(family,kind,protocol)
        try:
            if remaining is not socket._GLOBAL_DEFAULT_TIMEOUT:sock.settimeout(remaining)
            if source_address:sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:last=exc;sock.close()
    if last is not None:raise last
    raise TimeoutError('connection_deadline')

class HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._create_connection=connect

class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self,request):
        return self.do_open(HTTPSConnection,request,context=self._context)

def opener():return urllib.request.build_opener(HTTPSHandler())
