import os
import sys

# Set up paths
app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, app_dir)

# Import FastAPI app and convert to WSGI using a2wsgi
try:
    from a2wsgi import ASGIMiddleware
    from main import app
    application = ASGIMiddleware(app)
except Exception as e:
    def application(environ, start_response):
        status = '500 Internal Server Error'
        output = f'Error starting backend: {str(e)}'.encode('utf-8')
        response_headers = [('Content-type', 'text/plain; charset=utf-8'),
                            ('Content-Length', str(len(output)))]
        start_response(status, response_headers)
        return [output]
