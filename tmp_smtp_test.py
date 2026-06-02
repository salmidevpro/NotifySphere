import socket, smtplib, sys
host='smtp.gmail.com'
port=587
print('TESTING_TCP', host, port)
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(15)
try:
    s.connect((host, port))
    print('TCP_OK')
    try:
        banner = s.recv(1024).decode(errors='ignore')
        print('BANNER:', banner.strip())
    except Exception as e:
        print('BANNER_READ_FAIL', e)
    s.close()
except Exception as e:
    print('TCP_FAIL', repr(e))

print('\nTESTING_SMTP')
try:
    server = smtplib.SMTP(host, port, timeout=15)
    code, msg = server.ehlo()
    print('EHLO', code)
    try:
        server.starttls()
        print('STARTTLS_OK')
        code2, msg2 = server.ehlo()
        print('EHLO_AFTER_TLS', code2)
    except Exception as e:
        print('STARTTLS_FAIL', repr(e))
    try:
        server.quit()
    except Exception:
        pass
except Exception as e:
    print('SMTP_CONN_FAIL', repr(e))

sys.exit(0)