import requests
import frappe
from urllib.parse import urljoin
from datetime import datetime, timedelta

class SkriptBase:
    """Base API client for Skript"""
    
    def __init__(self, consumer_id, client_id, client_secret, api_url , api_scope="skript/ob-direct-data"):
        self.consumer_id = consumer_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_url = api_url
        # Follow the Enable Log tick on Bank Integration Setting (was always on).
        # Errors are still logged when it is off.
        self.enable_api_log = bool(frappe.db.get_single_value("Bank Integration Setting", "enable_log"))
        self.skript_api_scope = api_scope
        
        # Standard headers
        self.headers = {
            "Content-Type": "application/json"
        }
        self.is_auth_instance = False
    
    def get_valid_token(self, force_fresh=False):
        """Get a valid bearer token"""
        from bank_integration.skript.api.skript_authenticator import SkriptAuthenticator
        
        auth = SkriptAuthenticator(
            consumer_id=self.consumer_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
            api_url=self.api_url,
            api_scope=self.skript_api_scope
        )
        
        if force_fresh:
            auth.clear_cached_token()
        
        return auth.get_valid_token()
    
    def ensure_authenticated_headers(self, force_fresh=False):
        """Ensure headers have valid bearer token"""
        if force_fresh or "Authorization" not in self.headers:
            token = self.get_valid_token(force_fresh=force_fresh)
            if token:
                self.headers["Authorization"] = f"Bearer {token}"
            else:
                raise SkriptAPIError("Authentication failed", 401)
    
    def get(self, endpoint, params=None, headers=None):
        """GET request"""
        if not self.is_auth_instance:
            self.ensure_authenticated_headers()
        
        try:
            return self._make_request("GET", endpoint, params=params, headers=headers)
        except SkriptAPIError as e:
            if e.status_code == 401 and not self.is_auth_instance:
                # Token expired, refresh and retry
                self.ensure_authenticated_headers(force_fresh=True)
                return self._make_request("GET", endpoint, params=params, headers=headers)
            raise
    
    def post(self, endpoint, json=None, params=None, headers=None):
        """POST request"""
        if not self.is_auth_instance:
            self.ensure_authenticated_headers()
        
        try:
            return self._make_request("POST", endpoint, json=json, params=params, headers=headers)
        except SkriptAPIError as e:
            if e.status_code == 401 and not self.is_auth_instance:
                self.ensure_authenticated_headers(force_fresh=True)
                return self._make_request("POST", endpoint, json=json, params=params, headers=headers)
            raise
    
    def _make_request(self, method, endpoint, params=None, json=None, headers=None):
        """Make HTTP request"""
        url = self._build_url(endpoint)
        request_headers = {**self.headers, **(headers or {})}
        
        response = None
        
        try:
            response = requests.request(
                method, 
                url, 
                params=params, 
                json=json, 
                headers=request_headers,
                timeout=30
            )
            
            try:
                response_data = response.json()
            except ValueError:
                response_data = response.text
            
            # Log the request
            self.create_connection_log(
                status=str(response.status_code),
                message=str(response.text),
                response=response_data,
                method=method,
                url=url,
                payload=str(params) if json is None else str(json)
            )
            
            if response.status_code >= 400:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                raise SkriptAPIError(error_msg, response.status_code)
            
            return response_data
        
        except SkriptAPIError:
            raise
        except Exception as e:
            error_response = response.text if response else str(e)
            self.create_connection_log(
                status=response.status_code if response else 500,
                message="Error",
                response=error_response,
                method=method,
                url=url,
                payload=str(params) if json is None else str(json)
            )
            raise SkriptAPIError(str(e), getattr(response, 'status_code', 500))
    
    def _build_url(self, endpoint):
        """Build full URL with consumer_id"""
        # Replace {consumerId} placeholder
        endpoint = endpoint.replace("{consumerId}", self.consumer_id)
        
        # Ensure proper URL joining
        base_url = self.api_url.rstrip('/')
        endpoint = endpoint.lstrip('/')
        
        return f"{base_url}/{endpoint}"
    
    def create_connection_log(self, status, message, response=None, method=None, url=None, payload=None):
        """Create log entry"""
        try:
            status_string = "Success" if str(status).startswith("2") else "Error"
            if status_string == "Success" and not self.enable_api_log:
                return
            
            log = frappe.get_doc({
                "doctype": "Bank Integration Log",
                "status": status_string,
                "message": str(message),
                "response_data": str(response) if response else "",
                "request_data": str(payload) if payload else "",
                "url": url or "",
                "method": str(method) if method else "",
                "status_code": str(status)
            })
            log.insert(ignore_permissions=True)
            
        except Exception as e:
            frappe.log_error(f"Log creation error: {str(e)}", "Skript Log Error")


class SkriptAPIError(Exception):
    """Custom exception for Skript API errors"""
    def __init__(self, message, status_code=None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)
