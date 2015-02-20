import StringIO
import email
import urllib
import json
import hashlib
from base64 import b64decode

from django.http import MultiPartParser
from django.core.cache import get_cache

from util import convert_to_dict, convert_post_body_to_dict
from etag import get_etag_info
from jws import JWS, JWSException
from ..exceptions import OauthUnauthorized, OauthBadRequest, ParamError, BadRequest

from oauth_provider.utils import get_oauth_request, require_params
from oauth_provider.decorators import CheckOauth
from oauth_provider.store import store
from oauth2_provider.provider.oauth2.models import AccessToken

att_cache = get_cache('attachment_cache')

def parse(request, more_id=None):
    r_dict = {}
    # Build headers from request in request dict
    r_dict['headers'] = get_headers(request.META)
    
    # Traditional authorization should be passed in headers
    r_dict['auth'] = {}
    if 'Authorization' in r_dict['headers']:
        # OAuth will always be dict, not http auth. Set required fields for oauth module and type for authentication
        # module
        set_authorization(r_dict, request)     
    elif 'Authorization' in request.body or 'HTTP_AUTHORIZATION' in request.body:
        # Authorization could be passed into body if cross origin request
        r_dict['auth']['type'] = 'http'
    else:
        raise BadRequest("Request has no authorization")

    r_dict['params'] = {}
    # lookin for weird IE CORS stuff.. it'll be a post with a 'method' url param
    if request.method == 'POST' and 'method' in request.GET:
        bdy = convert_post_body_to_dict(request.body)
        # 'content' is in body for the IE cors POST
        if 'content' in bdy:
            r_dict['body'] = urllib.unquote(bdy.pop('content'))
        # headers are in the body too for IE CORS, we removes them
        r_dict['headers'].update(get_headers(bdy))
        for h in r_dict['headers']:
            bdy.pop(h, None)

        # remove extras from body
        bdy.pop('X-Experience-API-Version', None)
        bdy.pop('Content-Type', None)
        bdy.pop('If-Match', None)
        bdy.pop('If-None-Match', None)
        
        # all that should be left are params for the request, 
        # we adds them to the params object
        r_dict['params'].update(bdy)
        for k in request.GET:
            if k == 'method': # make sure the method param goes in the special method spot
                r_dict[k] = request.GET[k]
            else:
                r_dict['params'][k] = request.GET[k]
    # Just parse body for all non IE CORS stuff
    else:
        r_dict = parse_body(r_dict, request)
        # Update dict with any GET data
        r_dict['params'].update(request.GET.dict())

    # Method gets set for cors already
    if 'method' not in r_dict:
        # Differentiate GET and POST
        if request.method == "POST" and (request.path[6:] == 'statements' or request.path[6:] == 'statements/'):
            # Can have empty body for POST (acts like GET)
            if 'body' in r_dict:
                # If body is a list, it's a post
                if not isinstance(r_dict['body'], list):
                    if not isinstance(r_dict['body'], dict):
                        raise BadRequest("Cannot evaluate data into dictionary to parse -- Error: %s" % r_dict['body'])
                    # If actor verb and object not in body - means it's a GET or invalid POST
                    if not ('actor' in r_dict['body'] and 'verb' in r_dict['body'] and 'object' in r_dict['body']):
                        # If body keys are in get params - GET - else invalid request
                        if set(r_dict['body'].keys()).issubset(['statementId', 'voidedStatementId', 'agent', 'verb', 'activity', 'registration',
                            'related_activities', 'related_agents', 'since', 'until', 'limit', 'format', 'attachments', 'ascending']):
                            r_dict['method'] = 'GET'
                        else:
                            raise BadRequest("Statement is missing actor, verb, or object")
                    else:
                        r_dict['method'] = 'POST'
                else:
                    r_dict['method'] = 'POST'
            else:
                r_dict['method'] = 'GET'
        else:
            r_dict['method'] = request.method

    # Set if someone is hitting the statements/more endpoint
    if more_id:
        r_dict['more_id'] = more_id
    return r_dict

def set_authorization(r_dict, request):
    auth_params = r_dict['headers']['Authorization']
    # OAuth1 and basic http auth come in as string
    r_dict['auth']['endpoint'] = get_endpoint(request)
    if auth_params[:6] == 'OAuth ':
        oauth_request = get_oauth_request(request)
        
        # Returns HttpBadRequest if missing any params
        missing = require_params(oauth_request)            
        if missing:
            raise missing

        check = CheckOauth()
        e_type, error = check.check_access_token(request)

        if e_type and error:
            if e_type == 'auth':
                raise OauthUnauthorized(error)
            else:
                raise OauthBadRequest(error)

        # Consumer and token should be clean by now
        consumer = store.get_consumer(request, oauth_request, oauth_request['oauth_consumer_key'])
        token = store.get_access_token(request, oauth_request, consumer, oauth_request.get_parameter('oauth_token'))
        
        # Set consumer and token for authentication piece
        r_dict['auth']['oauth_consumer'] = consumer
        r_dict['auth']['oauth_token'] = token
        r_dict['auth']['type'] = 'oauth'
    elif auth_params[:7] == 'Bearer ':
        try:
            access_token = AccessToken.objects.get(token=auth_params[7:])
        except AccessToken.DoesNotExist:
            raise OauthUnauthorized("Access Token does not exist")
        else:
            if access_token.get_expire_delta() <= 0:
                raise OauthUnauthorized('Access Token has expired')
            r_dict['auth']['oauth_token'] = access_token
            r_dict['auth']['type'] = 'oauth2'
    else:        
        r_dict['auth']['type'] = 'http'    


def get_endpoint(request):
    # Used for OAuth scope
    endpoint = request.path[5:]
    # Since we accept with or without / on end
    if endpoint.endswith("/"):
        return endpoint[:-1]
    return endpoint   

def parse_attachment(r, request):
    # message = request.body
    # # Get boundary from header if not in body
    # if 'boundary' not in message[:message.index("--")]:
    #     if 'boundary' in request.META['CONTENT_TYPE']:
    #         boundary = request.META['CONTENT_TYPE'].split("boundary=")[1]
    #     else:
    #         raise BadRequest("Could not find the boundary for the multipart content")
    # else:
    #     # New line should always follow boundary header
    #     boundary = message[:message.index("--")].split("boundary=")[1].split("\n")[0]
    
    # # Python mail lib puts quotes around boundary, need to remove those to parse
    # if boundary.startswith('"'):
    #     boundary = boundary.strip('"')
    # boundary = "--" + boundary
    # message_parts = message.split(boundary)
    # # First item in list will be content-type/boundary info
    # if len(message_parts) < 2:
    #     raise ParamError("The content of the multipart request didn't contain a statement")
    
    # statement_part = message_parts[1]
    # # Replace mime-version part (that is generated by python email lib and is not needed)
    # stripped = statement_part.replace("\n", "").replace("\r", "").replace("MIME-Version: 1.0", "")
    
    # if "Content-Type:application/json" in stripped:
    #     stripped_parts = stripped.split("Content-Type:application/json")
    # elif "Content-Type: application/json" in stripped:
    #     stripped_parts = stripped.split("Content-Type: application/json")
    # else:
    #     raise ParamError("Content-Type of statement was not application/json")

    # try:
    #     r['body'] = json.loads(stripped_parts[1])
    # except Exception, e:
    #     raise ParamError("The content of the multipart request didn't contain a statement")

    # r['payload_sha2s'] = []
    # # Cycle through other parts to get attachments
    # for mp in message_parts[2:]:
    #     # Make check because last part will be newline at the end of the request
    #     if 'X-Experience-API-Hash' in mp:
    #         hash_parts = mp.split("X-Experience-API-Hash")
    #         hp = hash_parts[1]
    #         thehash = hp[hp.index(":")+1:hp.index("\n")].strip()
    #         r['payload_sha2s'].append(thehash)
    #         att_parts = hp.split(thehash)
    #         att_part = att_parts[len(att_parts)-1].replace("\r", "").replace("\n", "").strip()
            
    #         if hashlib.sha256(att_part).hexdigest() != thehash or \
    #             hashlib.sha256(b64decode(att_part)).hexdigest() != thehash:
    #                 if hashlib.sha384(att_part).hexdigest() != thehash or \
    #                     hashlib.sha384(b64decode(att_part)).hexdigest() != thehash:
    #                         if hashlib.sha512(att_part).hexdigest() != thehash or \
    #                             hashlib.sha512(b64decode(att_part)).hexdigest() != thehash:
    #                                 raise ParamError("The attachment content associated with sha %s does not match the sha" % thehash)

    #         att_cache.set(thehash, att_part)


    msg = email.message_from_string(request.body)
    boundary = msg.get_boundary()

    if msg.is_multipart():
        parts = msg.get_payload()
        stmt_part = parts.pop(0)
        if stmt_part.get('Content-Type', None) != "application/json":
            raise ParamError("Content-Type of statement was not application/json")
        try:
            r['body'] = json.loads(stmt_part.get_payload())
        except Exception, e:
            raise ParamError("Statement was not valid JSON")
        r['payload_sha2s'] = []
        for part in msg.get_payload():
            xhash = part.get('X-Experience-API-Hash', None)
            if not xhash:
                raise BadRequest("X-Experience-API-Hash header was missing from attachment")
            c_type = part['Content-Type']
            payload = part.get_payload()
            
            if hashlib.sha256(payload).hexdigest() != xhash and \
                hashlib.sha256(b64decode(payload)).hexdigest() != xhash:
                    if hashlib.sha384(payload).hexdigest() != xhash and \
                        hashlib.sha384(b64decode(payload)).hexdigest() != xhash:
                            if hashlib.sha512(payload).hexdigest() != xhash and \
                                hashlib.sha512(b64decode(payload)).hexdigest() != xhash:
                                    raise ParamError("The attachment content associated with sha %s does not match the sha" % xhash)

            r['payload_sha2s'].append(xhash)
            att_cache.set(xhash, payload)
    else:
        raise ParamError("This content was not multipart for the multipart request.")










    # See if the posted statements have attachments
    att_stmts = []
    if isinstance(r['body'], list):
        for s in r['body']:
            if 'attachments' in s:
                att_stmts.append(s)
    elif 'attachments' in r['body']:
        att_stmts.append(r['body'])
    if att_stmts:
        # find if any of those statements with attachments have a signed statement
        signed_stmts = [(s,a) for s in att_stmts for a in s.get('attachments', None) if a['usageType'] == "http://adlnet.gov/expapi/attachments/signature"]
        for ss in signed_stmts:
            attmnt = b64decode(att_cache.get(ss[1]['sha2']))
            jws = JWS(jws=attmnt)
            try:
                if not jws.verify() or not jws.validate(ss[0]):
                    raise BadRequest("The JSON Web Signature is not valid")
            except JWSException as jwsx:
                raise BadRequest(jwsx)

def parse_body(r, request):
    if request.method == 'POST' or request.method == 'PUT':
        # Parse out profiles/states if the POST dict is not empty
        if 'multipart/form-data' in request.META['CONTENT_TYPE']:
            if request.POST.dict().keys():
                r['params'].update(request.POST.dict())
                parser = MultiPartParser(request.META, StringIO.StringIO(request.raw_post_data),request.upload_handlers)
                post, files = parser.parse()
                r['files'] = files
        # If it is multipart/mixed, parse out all data
        elif 'multipart/mixed' in request.META['CONTENT_TYPE']: 
            parse_attachment(r, request)
        # Normal POST/PUT data
        else:
            if request.body:
                # profile uses the request body
                r['raw_body'] = request.body
                # Body will be some type of string, not necessarily JSON
                r['body'] = convert_to_dict(request.body)
            else:
                raise BadRequest("No body in request")
    return r

def get_headers(headers):
    r = {}
    if 'HTTP_UPDATED' in headers:
        r['updated'] = headers['HTTP_UPDATED']
    elif 'updated' in headers:
        r['updated'] = headers['updated']

    r['CONTENT_TYPE'] = headers.get('CONTENT_TYPE', '')
    if r['CONTENT_TYPE'] == '' and 'Content-Type' in headers:
        r['CONTENT_TYPE'] = headers['Content-Type']
    # FireFox automatically adds ;charset=foo to the end of headers. This will strip it out
    if ';' in r['CONTENT_TYPE']:
        r['CONTENT_TYPE'] = r['CONTENT_TYPE'].split(';')[0]

    r['ETAG'] = get_etag_info(headers, required=False)
    if 'HTTP_AUTHORIZATION' in headers:
        r['Authorization'] = headers.get('HTTP_AUTHORIZATION', None)
    elif 'Authorization' in headers:
        r['Authorization'] = headers.get('Authorization', None)

    if 'Accept_Language' in headers:
        r['language'] = headers.get('Accept_Language', None)
    elif 'Accept-Language' in headers:
        r['language'] = headers['Accept-Language']
    return r
