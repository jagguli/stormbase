import tornado
import tornado.web
from tornado.options import options
from stormbase import session
import urllib
import logging
import traceback
import os

from tornado import gen
from tornado import web
from time import time
from stormbase.util import dump_json
from tornado.curl_httpclient import CurlAsyncHTTPClient
from urlparse import urlparse
from tornado.httpclient import AsyncHTTPClient
from tornado import stack_context

CACHID = time()


def async_engine(func):
    return web.asynchronous(gen.engine(func))


class ProxyCurlAsyncHTTPClient(CurlAsyncHTTPClient):
    fetch_args = None

    def initialize(self, io_loop=None, max_clients=10,
                   max_simultaneous_connections=None, **kwargs):
        super(ProxyCurlAsyncHTTPClient, self).initialize(
            io_loop,
            max_clients,
            max_simultaneous_connections)
        self.fetch_args = kwargs

    def fetch(self, request, callback, **kwargs):
        kwargs.update(self.fetch_args)
        if 'no_proxy' in kwargs.keys():
            logging.debug("found no_proxy:%s" % str(kwargs['no_proxy']))
            if isinstance(request, str):
                if urlparse(request).hostname in kwargs['no_proxy']:
                    del kwargs['proxy_host']
                    del kwargs['proxy_port']
            del kwargs['no_proxy']
        super(
            ProxyCurlAsyncHTTPClient, self).fetch(request, callback, **kwargs)

proxy_url = os.getenv('http_proxy', '')

if proxy_url:
    parsed = urlparse(proxy_url)
    AsyncHTTPClient.configure(
        ProxyCurlAsyncHTTPClient, proxy_host=parsed.hostname,
        proxy_port=parsed.port, proxy_username=parsed.username,
        proxy_password=parsed.password, no_proxy=['localhost'])


class StormBaseHandler(tornado.web.RequestHandler):
    session = None
    def initialize(self, *args, **kwargs):
        self.db = self.application.db
        if hasattr(self.application,'session_manager'):
            self.session = session.Session(
                self.application.session_manager, self)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.render_method = kwargs.get('render_method', 'html')

    def get_current_user(self):
        userid = self.get_secure_cookie("user")
        user_obj = None
        if self.session:
            user_obj = self.session.get(userid, None)
        logging.debug("LOGINSTATUS: %s, %s" % (userid, user_obj is not None))
        if not userid or not user_obj:
            return None
        return user_obj

    def _default_template_variables(self, kwargs):
        kwargs['options'] = options
        kwargs['static_url'] = self.static_url
        kwargs['url'] = self.url
        kwargs['xsrf_form_html'] = self.xsrf_form_html
        kwargs['xsrf_token'] = self.xsrf_token
        kwargs['add_javascript'] = self.add_javascript
        kwargs['add_css'] = self.add_css
        kwargs['current_user'] = self.current_user

    def end(self, *args, **kwargs):
        if self.render_method == 'html':
            return self.render(*args, **kwargs)
        elif self.render_method == 'json':
            return self.render_json(kwargs)
        raise Exception("Unknown render_method:%s" % self.render_method)

    def render(self, template_name, finish=True, **kwargs):
        self._default_template_variables(kwargs)
        template = self.application.jinja_env.get_template(template_name)
        self.write(template.render(kwargs))
        if finish:
            self.finish()

    def render_string_template(self, string_template, **kwargs):
        self._default_template_variables(kwargs)
        template = self.application.jinja_env.from_string(string_template)
        return template.render(**kwargs).strip()

    def render_json(self, data, finish=True):
        self.write(dump_json(data))
        if finish:
            self.finish()

    def static_url(self, url):
        return urllib.basejoin(options.static_root, url)

    def url(self, url):
        return urllib.basejoin(options.root, url)

    def add_css(self, css, cache=True, vendor=False, **kwargs):
        if css.startswith('http'):
            path = css
        elif vendor:
            path = urllib.basejoin(options.vendor_css_root, css)
        else:
            path = urllib.basejoin(options.static_root, 'css/')
            path = urllib.basejoin(path, css)
        cachestring = ('' if cache or not options.debug
                       else '?cacheid=%s' % CACHID)
        extra_params = ""
        for item in kwargs.iteritems():
            extra_params += '"%s"="%s" ' % item
        return """<link rel="stylesheet" href="%s%s" type="text/css" %s/>""" \
            % (path, cachestring, extra_params)

    def add_javascript(self, script, cache=True, vendor=False, **kwargs):
        if script.startswith('http'):
            path = script
        elif vendor:
            path = urllib.basejoin(options.vendor_script_root, script)
        else:
            path = urllib.basejoin(options.script_root, script)
        cachestring = ('' if cache or not options.debug
                       else '?cacheid=%s' % CACHID)
        return """<script src="%s%s" type="text/javascript"></script>""" \
            % (path, cachestring)

    #def get_error_html(self, status_code, **kwargs):
    #    self.render('error.html', status_code=status_code,
    #        message=httplib.responses[status_code])

    def error(self, exception):
        try:
            logging.error("Handler Exception:  %s." % str(exception))
            traceback.print_exc()
        except Exception, e:
            print "Error logging." + str(e)

    def get_real_ip(self):
        try:
            self.real_ip = self.request.headers.get(
                'X-Real-Ip',
                self.request.headers.get('X-Forwarded-For', None))
            logging.info(
                "Request from " + str(self.real_ip) + str(self.__class__))
        except Exception, e:
            self.error(e)

    @stack_context.contextlib.contextmanager
    def on_async_error(self):
        try:
            yield
        except Exception as e:
            logging.error("exception in asynchronous operation", exc_info=True)
            self.write(str(e))

    def write_error(self, status_code, **kwargs):
        import traceback
        if self.settings.get("debug") and "exc_info" in kwargs:
            exc_info = kwargs["exc_info"]
            trace_info = ''.join(["%s<br/>" % line for line in
                                  traceback.format_exception(*exc_info)])
            request_info = ''.join(["<strong>%s</strong>: %s<br/>" %
                                    (k, self.request.__dict__[k])
                                    for k in self.request.__dict__.keys()])
            error = exc_info[1]

            self.set_header('Content-Type', 'text/html')
            self.finish("""<html>
                             <title>%s</title>
                             <body>
                                <h2 class="error">Error</h2>
                                <p>%s</p>
                                <h2>Traceback</h2>
                                <p>%s</p>
                                <h2>Request Info</h2>
                                <p>%s</p>
                             </body>
                           </html>""" % (error, error,
                                         trace_info, request_info))


def get_static_handlers():
    static_root = options.static_root
    static_root = (static_root[1:] if
                   static_root.startswith(os.path.sep) else static_root)
    cwd = os.getcwd()
    static_root = os.path.join(cwd, static_root)

    return [
        (r'/static/js/(.*)', tornado.web.StaticFileHandler,
         {'path':os.path.join(cwd, 'src/javascript')}),
        (r'/static/vendor/(.*)', tornado.web.StaticFileHandler,
         {'path':os.path.join(cwd, '../../var/static/vendor')}),
        (r'/static/(.*)', tornado.web.StaticFileHandler,
         {'path':static_root}),
        (r'/favicon.ico(.*)', tornado.web.StaticFileHandler,
         {'path':os.path.join(static_root, 'img/favicon.ico')})
        ]
