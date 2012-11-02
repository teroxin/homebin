import smtpd, asyncore, re, os

class SMTPSpeaker(smtpd.SMTPServer):
    def process_message(self, peer, mailfrom, rcpttos, data):
        find = re.search('Content-Type: text/plain.*?\n(.*)', data, re.DOTALL)
        if find and len(find.groups())>0:
            msg = find.group(1)
            print "The message is: %s" % msg
            msg_clean = re.sub('[",\',!,@,#,$,%,^,&,*,(,)]', '', msg)
            os.system('say "%s" ' % msg_clean)
        else:
            print "no message found"

#put your IP in here!
server = SMTPSpeaker(('10.0.0.200', 25), None)
asyncore.loop()
