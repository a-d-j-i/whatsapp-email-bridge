#!/usr/bin/python

# Copyright 2015, Axel Angel, under the GPLv3 license.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, signal
import datetime, sys
import smtplib
import base64
import yaml
import threading
import Queue
import socket
import time
import asyncore
import atexit
import tempfile
from parse import parse
from email.mime.text import MIMEText
from email.parser import Parser
from email.utils import formatdate
from smtpd import SMTPChannel, SMTPServer
from html2text import html2text

from yowsup.common import YowConstants
from yowsup import env
from yowsup.layers.auth import YowCryptLayer, YowAuthenticationProtocolLayer, \
        AuthError
from yowsup.layers.coder import YowCoderLayer
from yowsup.layers import YowLayerEvent
from yowsup.layers.interface import YowInterfaceLayer, ProtocolEntityCallback
from yowsup.layers.logger import YowLoggerLayer
from yowsup.layers.network import YowNetworkLayer
from yowsup.layers.protocol_acks import YowAckProtocolLayer
from yowsup.layers.protocol_acks.protocolentities \
        import OutgoingAckProtocolEntity
from yowsup.layers.protocol_media import YowMediaProtocolLayer
from yowsup.layers.protocol_media.protocolentities \
        import ImageDownloadableMediaMessageProtocolEntity
from yowsup.layers.protocol_media.protocolentities \
        import LocationMediaMessageProtocolEntity
from yowsup.layers.protocol_media.protocolentities \
        import VCardMediaMessageProtocolEntity
from yowsup.layers.protocol_media.protocolentities \
        import RequestUploadIqProtocolEntity
from yowsup.layers.protocol_media.mediauploader import MediaUploader
from yowsup.layers.protocol_iq import YowIqProtocolLayer
from yowsup.layers.protocol_messages import YowMessagesProtocolLayer
from yowsup.layers.protocol_messages.protocolentities \
        import TextMessageProtocolEntity
from yowsup.layers.protocol_receipts import YowReceiptProtocolLayer
from yowsup.layers.protocol_receipts.protocolentities \
        import OutgoingReceiptProtocolEntity
from yowsup.layers.protocol_presence import YowPresenceProtocolLayer
from yowsup.layers.stanzaregulator import YowStanzaRegulator
from yowsup.stacks import YowStack, YOWSUP_CORE_LAYERS


class MailLayer(YowInterfaceLayer):
    def __init__(self):
        YowInterfaceLayer.__init__(self)

    @ProtocolEntityCallback("success")
    def onSuccess(self, entity):
        print "<= WhatsApp: Logged in"

    @ProtocolEntityCallback("failure")
    def onFailure(self, entity):
        print "<= WhatsApp: Failure %s" % (entity)

    @ProtocolEntityCallback("notification")
    def onNotification(self, notification):
        print "<= WhatsApp: Notification %s" % (notification)

    @ProtocolEntityCallback("message")
    def onMessage(self, mEntity):
        if not mEntity.isGroupMessage():
            if mEntity.getType() == 'text':
                self.onTextMessage(mEntity)
            elif mEntity.getType() == 'media':
                self.onMediaMessage(mEntity)
        else:
            src = mEntity.getFrom()
            print "<= WhatsApp: <- %s GroupMessage" % (src)

    @ProtocolEntityCallback("receipt")
    def onReceipt(self, entity):
        ack = OutgoingAckProtocolEntity(entity.getId(), "receipt",
                entity.getType(), entity.getFrom())
        self.toLower(ack)

    def sendEmail(self, mEntity, subject, content):
        timestamp = mEntity.getTimestamp()
        srcShort = mEntity.getFrom(full = False)
        replyAddr = config['reply'].format(srcShort)
        dst = config['outgoing']['sendto']

        formattedDate = datetime.datetime.fromtimestamp(timestamp) \
                                         .strftime('%d/%m/%Y %H:%M')
        content2 = "%s\n\nAt %s by %s (%s) isBroadCast=%s" \
                % (content, formattedDate, srcShort, mEntity.getParticipant(),
                    mEntity.isBroadcast())

        msg = MIMEText(content2, 'plain', 'utf-8')
        msg['To'] = "WhatsApp <%s>" % (dst)
        msg['From'] = "%s <%s>" % (srcShort, mEntity.getParticipant())
        msg['Reply-To'] = "%s <%s>" % (mEntity.getParticipant(), replyAddr)
        msg['Subject'] = subject
        msg['Date'] = formatdate(timestamp)

        confout = config['outgoing']
        if confout.get('ssl', False):
            s_class = smtplib.SMTP_SSL
        else:
            s_class = smtplib.SMTP

        s = s_class(confout['host'], confout.get('port', 25))

        if confout.get('smtp_user', None):
            s.login(confout.get('smtp_user'), confout.get('smtp_pass'))

        if not confout.get('force_startssl', True):
            try:
                s.starttls() # Some servers require it, let's try
            except smtplib.SMTPException:
                print "<= Mail: Server doesn't support STARTTLS"
                if confout.get('force_starttls'):
                    raise

        s.sendmail(dst, [dst], msg.as_string())
        s.quit()
        print "=> Mail: %s -> %s" % (replyAddr, dst)

    def onTextMessage(self, mEntity):
        receipt = OutgoingReceiptProtocolEntity(mEntity.getId(),
                mEntity.getFrom())

        src = mEntity.getFrom()
        print("<= WhatsApp: <- %s Message" % (src))

        content = mEntity.getBody()
        self.sendEmail(mEntity, content, content)
        self.toLower(receipt)

    def onMediaMessage(self, mEntity):
        id = mEntity.getId()
        src = mEntity.getFrom()
        tpe = mEntity.getMediaType()
        url = getattr(mEntity, 'url', None)

        print("<= WhatsApp: <- Media %s (%s)" % (tpe, src))

        content = "Received a media of type: %s\n" % (tpe)
        content += "URL: %s\n" % (url)
        content += str(mEntity)
        self.sendEmail(mEntity, "Media: %s" % (tpe), content)

        receipt = OutgoingReceiptProtocolEntity(id, src)
        self.toLower(receipt)


class YowsupMyStack(object):
    def __init__(self, credentials):
        env.CURRENT_ENV = env.S40YowsupEnv()
        self.layer = MailLayer()
        layers = (
            self.layer,
            (YowAuthenticationProtocolLayer, YowMessagesProtocolLayer,
                YowReceiptProtocolLayer, YowAckProtocolLayer,
                YowMediaProtocolLayer, YowIqProtocolLayer,
                YowPresenceProtocolLayer)
            ) + YOWSUP_CORE_LAYERS

        self.stack = YowStack(layers)
        self.stack.setProp(YowAuthenticationProtocolLayer.PROP_CREDENTIALS,
                credentials)
        self.stack.setProp(YowNetworkLayer.PROP_ENDPOINT,
                YowConstants.ENDPOINTS[0])
        self.stack.setProp(YowCoderLayer.PROP_DOMAIN, YowConstants.DOMAIN)
        self.stack.setProp(YowCoderLayer.PROP_RESOURCE,
                env.CURRENT_ENV.getResource())

    def startInputThread(self):
        print "Starting input thread"
        confinc = config['ingoing']
        if confinc['with'] == "LMTP":
            sockpath = confinc['socket']
            self.server = YoLMTPServer(self.layer, sockpath, None)
            atexit.register(clean_lmtp)
        elif confinc['with'] == "SMTP":
            host = confinc['host']
            port = confinc['port']
            self.server = YoSMTPServer(self.layer, (host, port), None)
        elif confinc['with'] == "POP3":
           self.server = Pop3Client(self.layer, confinc)
        elif confinc['with'] == "IMAP":
           self.server = ImapClient(self.layer, confinc)
        else:
            raise Exception("Unknown ingoing type")


    def start(self):

        self.startInputThread()
        self.server.start()

        self.stack.broadcastEvent(
                YowLayerEvent(YowNetworkLayer.EVENT_STATE_CONNECT))

        try:
            while True:
                self.stack.loop( timeout = 10, count = 1 )
                while self.server.loop():
                    pass
        except AuthError as e:
            print("Authentication Error: %s" % e.message)


class LMTPChannel(SMTPChannel):
  # LMTP "LHLO" command is routed to the SMTP/ESMTP command
  def smtp_LHLO(self, arg):
    self.smtp_HELO(arg)

  def smtp_EHLO(self, arg):
    self.smtp_HELO(arg)


class MailParserMixin():

    def __init__(self, yowsup):
        self._yowsup = yowsup

    def send_yowsup(self, phone, data):
        m = Parser().parsestr(data)
        try:
            txt = mail_to_txt(m)
        except Exception as e:
            return "501 malformed content: %s" % (str(e))

        jid = normalizeJid(phone)

        # send text, if any
        if len(txt.strip()) > 0:
            msg = TextMessageProtocolEntity(txt, to = jid)
            print "=> WhatsApp: -> %s" % (jid)
            self._yowsup.toLower(msg)

        # send media that were attached pieces
        if m.is_multipart():
            for pl in getattr(m, '_payload', []):
                self.handle_forward_media(jid, pl)
        return False


    def handle_forward_media(self, jid, pl):
            jid = normalizeJid(phone)
            # send text, if any
            if len(txt.strip()) > 0:
                msg = TextMessageProtocolEntity(txt, to = jid)
                print "=> WhatsApp: -> %s" % (jid)
                self._yowsup.toLower(msg)

            # send media that were attached pieces
            if m.is_multipart():
                for pl in getattr(m, '_payload', []):
                    self.handle_forward_media(jid, pl)

    def handle_forward_media(self, jid, pl):
        ct = pl.get('Content-Type', 'None')
        ct1 = ct.split('/', 1)[0]
        iqtp = None
        if ct1 == 'text':
            return # this is the body, probably
        if ct1 == 'image':
            iqtp = RequestUploadIqProtocolEntity.MEDIA_TYPE_IMAGE
        if ct1 == 'audio':
            iqtp = RequestUploadIqProtocolEntity.MEDIA_TYPE_AUDIO
        if ct1 == 'video':
            iqtp = RequestUploadIqProtocolEntity.MEDIA_TYPE_VIDEO
        if ct.startswith('multipart/alternative'): # recursive content
            for pl2 in pl._payload:
                self.handle_forward_media(jid, pl2)
        if iqtp == None:
            print "<= Mail: Skip unsupported attachement type %s" % (ct)
            return

        print "<= Mail: Forward attachement %s" % (ct1)
        data = mail_payload_decoded(pl)
        tmpf = tempfile.NamedTemporaryFile(prefix='whatsapp-upload_',
                delete=False)
        tmpf.write(data)
        tmpf.close()
        fpath = tmpf.name
        # FIXME: need to close the file!

        entity = RequestUploadIqProtocolEntity(iqtp, filePath=fpath)
        def successFn(successEntity, originalEntity):
            return self.onRequestUploadResult(
                    jid, fpath, successEntity, originalEntity)
        def errorFn(errorEntity, originalEntity):
            return self.onRequestUploadError(
                    jid, fpath, errorEntity, originalEntity)

        self._yowsup._sendIq(entity, successFn, errorFn)

    def onRequestUploadResult(self, jid, fpath, successEntity, originalEntity):
        if successEntity.isDuplicate():
            url = successEntity.getUrl()
            ip = successEntity.getIp()
            print "<= WhatsApp: upload duplicate %s, from %s" % (fpath, url)
            self.send_uploaded_media(fpath, jid, url, ip)
        else:
            ownjid = self._yowsup.getOwnJid()
            mediaUploader = MediaUploader(jid, ownjid, fpath,
                                      successEntity.getUrl(),
                                      successEntity.getResumeOffset(),
                                      self.onUploadSuccess,
                                      self.onUploadError,
                                      self.onUploadProgress,
                                      async=False)
            print "<= WhatsApp: start upload %s, into %s" \
                    % (fpath, successEntity.getUrl())
            mediaUploader.start()

    def onUploadSuccess(self, fpath, jid, url):
        print "WhatsApp: -> upload success %s" % (fpath)
        self.send_uploaded_media(fpath, jid, url)

    def onUploadError(self, fpath, jid=None, url=None):
        print "WhatsApp: -> upload failed %s" % (fpath)
        ownjid = self._yowsup.getOwnJid()
        fakeEntity = TextMessageProtocolEntity("", _from = ownjid)
        self._yowsup.sendEmail(fakeEntity, "WhatsApp upload failed",
                "File: %s" % (fpath))

    def onUploadProgress(self, fpath, jid, url, progress):
        print "WhatsApp: -> upload progression %s for %s, %d%%" \
                % (fpath, jid, progress)

    def send_uploaded_media(self, fpath, jid, url, ip = None):
        entity = ImageDownloadableMediaMessageProtocolEntity.fromFilePath(
                fpath, url, ip, jid)
        self._yowsup.toLower(entity)

    def onRequestUploadError(self, jid, fpath, errorEntity, originalEntity):
        print "WhatsApp: -> upload request failed %s" % (fpath)
        self._yowsup.sendEmail(errorEntity, "WhatsApp upload request failed",
                "File: %s" % (fpath))

class NetClient( MailParserMixin ):
    def __init__(self, yowsup, confinc):
        self.host = confinc['host']
        self.port = confinc['port']
        self.user = confinc['user']
        self.password = confinc['pass']
        self.dest = confinc['dest']
        if 'delay' in confinc:
            self.delay = confinc['delay']
        else:
            self.delay = 360
        self.use_ssl = ( 'use_ssl' in confinc )

        self.messageQueue = Queue.Queue()
        self._yowsup = yowsup
        self.thread = threading.Thread(target=self.worker)

    def start(self):
        self.thread.daemon = True
        self.thread.start()

    def loop(self):
        try:
            while True:
                msg = self.messageQueue.get(False)
                if not msg:
                    break
                self.send_yowsup(self.dest, msg)
        except Queue.Empty:
            pass

class Pop3Client( NetClient ):

    def worker(self):
        import poplib
        while True:
            if self.use_ssl:
                M = poplib.POP3_SSL( self.host, self.port )
            else:
                M = poplib.POP3( self.host, self.port )
            M.user( self.user )
            M.pass_( self.password )
            numMessages = len( M.list()[ 1 ] )
            for mList in range(numMessages) :
                for msg in M.retr( mList + 1 )[1]:
                    self.messageQueue.put( msg )
                # to avoid resending
                M.dele( mList + 1 )
            time.sleep( self.delay )

class ImapClient( NetClient ):

    def worker(self):
        import imaplib
        while True:
            if self.use_ssl:
                M = imaplib.IMAP4_SSL( self.host, self.port )
            else:
                M = imaplib.IMAP4( self.host, self.port )
            M.login( self.user, self.password )
            M.select()
            typ, data = M.search( None, '(UNSEEN)' )
            for num in data[0].split():
                typ, data = M.fetch(num, '(RFC822)')
                self.messageQueue.put( data[0][1] )
            M.close()
            M.logout()
            time.sleep( self.delay )
        


class MailServer(SMTPServer, MailParserMixin):

    def start(self):
        pass 

    def handle_accept(self):
        conn, addr = self.accept()
        channel = LMTPChannel(self, conn, addr)

    def process_message(self, peer, mailfrom, rcpttos, data):
        m = Parser().parsestr(data)
        print "<= Mail: %s -> %s" % (mailfrom, rcpttos)

        try:
            txt = mail_to_txt(m)
        except Exception as e:
            return "501 malformed content: %s" % (str(e))

        for dst in rcpttos:
            try:
                (phone,) = parse(config.get('reply'), dst)
            except TypeError:
                print "malformed dst: %s" % (dst)
                return "501 malformed recipient: %s" % (dst)

            ret = self.send_yowsup(phone, data)
            if ret:
                return ret


class YoLMTPServer(MailServer):
    def __init__(self, yowsup, localaddr, remoteaddr):
        # code taken from original SMTPServer code
        self._yowsup = yowsup
        self._localaddr = localaddr
        self._remoteaddr = remoteaddr
        asyncore.dispatcher.__init__(self)
        try:
            self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # try to re-use a server port if possible
            self.set_reuse_addr()
            self.bind(localaddr)
            self.listen(5)
        except:
            # cleanup asyncore.socket_map before raising
            self.close()
            raise

class YoSMTPServer(MailServer):
    def __init__(self, yowsup, localaddr, remoteaddr):
        # code taken from original SMTPServer code
        self._yowsup = yowsup
        self._localaddr = localaddr
        self._remoteaddr = remoteaddr
        asyncore.dispatcher.__init__(self)
        try:
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            # try to re-use a server port if possible
            self.set_reuse_addr()
            self.bind(localaddr)
            self.listen(5)
        except:
            # cleanup asyncore.socket_map before raising
            self.close()
            raise


def mail_payload_decoded(pl):
    t = pl.get_payload()
    if pl.get('Content-Transfer-Encoding', None) == "base64":
        t = base64.b64decode(t)
    return t

def mail_to_txt(m):
    if not m.is_multipart():
        # simple case for text/plain
        return mail_payload_decoded(m)

    else:
        # handle when there are attachements (take first text/plain)
        for pl in m._payload:
            if "text/plain" in pl.get('Content-Type', None):
                return mail_payload_decoded(pl)
        # otherwise take first text/html
        for pl in m._payload:
            if "text/html" in pl.get('Content-Type', None):
                return html2text(mail_payload_decoded(pl))
        # otherwise search into recursive message
        for pl in m._payload:
            try:
                if "multipart/alternative" in pl.get('Content-Type', None):
                    return mail_to_txt(pl)
            except:
                continue # continue to next attachment

        raise Exception("No text could be extracted found")

def loadConfig(fpath):
    with open(fpath, 'rb') as fd:
        config = yaml.load(fd)
        return config

def normalizeJid(number):
    if '@' in number:
        return number
    elif "-" in number:
        return "%s@g.us" % number

    return "%s@s.whatsapp.net" % number

def clean_lmtp():
    try:
        os.unlink(config['ingoing'].get('socket'))
    except OSError:
        pass

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--config', default='config.yaml',
            help='configuration file path')
    args = p.parse_args()

    print "Parsing config: %s" % (args.config)
    config = loadConfig(args.config)

    print "Starting"
    confwhats = config['whatsapp']
    stack = YowsupMyStack((confwhats.get('phone'), confwhats.get('password')))
    print "Connecting"
    try:
        stack.start()
    except KeyboardInterrupt:
        print "Terminated by user"
