"""
Cursor Cloud Agents API Client

Official documentation: https://cursor.com/docs/cloud-agent/api/endpoints
API base URL: https://api.cursor.com/v1

This client provides a minimal interface to the Cursor Cloud Agents API
for the research pipeline orchestrator.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin

# Type alias for HTTP client (injected for testing)
HttpClient = Callable[[str, str, Optional[dict], Optional[dict]], tuple[int, dict]]


@dataclass
class RunStatus:
    """Status of a cloud agent run."""
    CREATING = "CREATING"
    RUNNING = "RUNNING"
    WAITING_FOR_BACKGROUND_WORK = "WAITING_FOR_BACKGROUND_WORK"
    FINISHED = "FINISHED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    
    @classmethod
    def is_active(cls, status: str) -> bool:
        return status in (cls.CREATING, cls.RUNNING, cls.WAITING_FOR_BACKGROUND_WORK)
    
    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in (cls.FINISHED, cls.ERROR, cls.CANCELLED, cls.EXPIRED)
    
    @classmethod
    def is_success(cls, status: str) -> bool:
        return status == cls.FINISHED


@dataclass
class Model:
    """Model information from GET /v1/models."""
    id: str
    display_name: str
    description: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    parameters: list[dict] = field(default_factory=list)
    variants: list[dict] = field(default_factory=list)


@dataclass
class AgentRun:
    """Cloud agent run information."""
    agent_id: str
    run_id: str
    status: str
    agent_url: Optional[str] = None
    branches: list[dict] = field(default_factory=list)
    duration_ms: Optional[int] = None
    result_text: Optional[str] = None


class CursorAPIError(Exception):
    """Error from Cursor API."""
    def __init__(self, code: str, message: str, status_code: int = 0):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"{code}: {message}")


def _default_http_client(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> tuple[int, dict]:
    """Default HTTP client using requests library."""
    import requests
    
    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        json=json_body,
        timeout=60,
    )
    
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    
    return response.status_code, body


class CursorAPIClient:
    """
    Client for Cursor Cloud Agents API v1.
    
    Official docs: https://cursor.com/docs/cloud-agent/api/endpoints
    """
    
    DEFAULT_BASE_URL = "https://api.cursor.com/v1"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        http_client: Optional[HttpClient] = None,
    ):
        """
        Initialize the API client.
        
        Args:
            api_key: Cursor API key. If not provided, reads from CURSOR_API_KEY env var.
            base_url: API base URL. Defaults to https://api.cursor.com/v1
            http_client: HTTP client function for testing. Defaults to requests-based client.
        """
        self.api_key = api_key or os.environ.get("CURSOR_API_KEY")
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._http = http_client or _default_http_client
        self._models_cache: Optional[list[Model]] = None
    
    def _headers(self) -> dict:
        """Build request headers with authentication."""
        if not self.api_key:
            raise CursorAPIError("auth_error", "CURSOR_API_KEY not set")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
    
    def _request(
        self,
        method: str,
        endpoint: str,
        body: Optional[dict] = None,
    ) -> dict:
        """Make an API request."""
        url = urljoin(self.base_url + "/", endpoint.lstrip("/"))
        status_code, response = self._http(method, url, self._headers(), body)
        
        if status_code >= 400:
            error = response.get("error", {})
            raise CursorAPIError(
                code=error.get("code", "unknown_error"),
                message=error.get("message", str(response)),
                status_code=status_code,
            )
        
        return response
    
    def list_models(self, force_refresh: bool = False) -> list[Model]:
        """
        List available models from GET /v1/models.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#list-models
        
        Returns:
            List of available models with their parameters and variants.
        """
        if self._models_cache is not None and not force_refresh:
            return self._models_cache
        
        response = self._request("GET", "/models")
        
        models = []
        for item in response.get("items", []):
            models.append(Model(
                id=item["id"],
                display_name=item.get("displayName", item["id"]),
                description=item.get("description"),
                aliases=item.get("aliases", []),
                parameters=item.get("parameters", []),
                variants=item.get("variants", []),
            ))
        
        self._models_cache = models
        return models
    
    def validate_model(self, model_id: str) -> tuple[bool, Optional[Model]]:
        """
        Validate that a model ID is available.
        
        Args:
            model_id: Model ID to validate
            
        Returns:
            Tuple of (is_valid, model_info)
        """
        models = self.list_models()
        
        for model in models:
            if model.id == model_id:
                return True, model
            if model_id in model.aliases:
                return True, model
        
        return False, None
    
    def validate_model_params(
        self,
        model_id: str,
        params: list[dict],
    ) -> tuple[bool, list[str]]:
        """
        Validate model parameters against the model's allowed parameters.
        
        Args:
            model_id: Model ID
            params: List of {id, value} parameter entries
            
        Returns:
            Tuple of (all_valid, list_of_errors)
        """
        is_valid, model = self.validate_model(model_id)
        if not is_valid or model is None:
            return False, [f"Model '{model_id}' not found"]
        
        errors = []
        allowed_params = {p["id"]: p for p in model.parameters}
        
        for param in params:
            param_id = param.get("id")
            param_value = param.get("value")
            
            if param_id not in allowed_params:
                errors.append(
                    f"Parameter '{param_id}' is not listed for model '{model_id}'. "
                    f"Parameters listed for this model: {sorted(allowed_params)}"
                )
                continue
            
            allowed_values = [v["value"] for v in allowed_params[param_id].get("values", [])]
            if param_value not in allowed_values:
                errors.append(
                    f"Value '{param_value}' is not permitted for parameter "
                    f"'{param_id}' of model '{model_id}'. "
                    f"Permitted values: {allowed_values}"
                )
        
        return len(errors) == 0, errors
    
    def create_agent(
        self,
        prompt_text: str,
        repo_url: str,
        starting_ref: str = "main",
        model_id: Optional[str] = None,
        model_params: Optional[list[dict]] = None,
        name: Optional[str] = None,
        auto_create_pr: bool = False,
    ) -> AgentRun:
        """
        Create a cloud agent and start its initial run.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#create-an-agent
        
        Args:
            prompt_text: The instruction text for the agent
            repo_url: Repository URL (e.g., https://github.com/org/repo)
            starting_ref: Branch name or commit SHA to start from
            model_id: Model ID from GET /v1/models. Omit for configured default.
            model_params: Optional model parameters [{id, value}, ...]
            name: Display name for the agent (max 100 chars)
            auto_create_pr: Whether to auto-create PR on completion
            
        Returns:
            AgentRun with agent_id, run_id, and initial status
        """
        body: dict[str, Any] = {
            "prompt": {"text": prompt_text},
            "repos": [{"url": repo_url, "startingRef": starting_ref}],
            "autoCreatePR": auto_create_pr,
        }
        
        if name:
            body["name"] = name[:100]
        
        if model_id:
            body["model"] = {"id": model_id}
            if model_params:
                body["model"]["params"] = model_params
        
        response = self._request("POST", "/agents", body)
        
        agent = response.get("agent", {})
        run = response.get("run", {})
        
        return AgentRun(
            agent_id=agent.get("id", ""),
            run_id=run.get("id", ""),
            status=run.get("status", "UNKNOWN"),
            agent_url=agent.get("url"),
        )
    
    def get_run(self, agent_id: str, run_id: str) -> AgentRun:
        """
        Get the status of a specific run.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#get-a-run
        
        Args:
            agent_id: Agent ID (bc-...)
            run_id: Run ID (run-...)
            
        Returns:
            AgentRun with current status and results
        """
        response = self._request("GET", f"/agents/{agent_id}/runs/{run_id}")
        
        git_info = response.get("git", {})
        
        return AgentRun(
            agent_id=agent_id,
            run_id=run_id,
            status=response.get("status", "UNKNOWN"),
            branches=git_info.get("branches", []),
            duration_ms=response.get("durationMs"),
            result_text=response.get("text"),
        )
    
    def poll_run_until_complete(
        self,
        agent_id: str,
        run_id: str,
        interval_seconds: int = 30,
        max_attempts: int = 120,
        on_status: Optional[Callable[[str, int], None]] = None,
    ) -> AgentRun:
        """
        Poll a run until it reaches a terminal status.
        
        Args:
            agent_id: Agent ID
            run_id: Run ID
            interval_seconds: Seconds between polls
            max_attempts: Maximum number of poll attempts
            on_status: Optional callback(status, attempt) for progress updates
            
        Returns:
            Final AgentRun with terminal status
            
        Raises:
            CursorAPIError: If max attempts exceeded or API error
        """
        for attempt in range(1, max_attempts + 1):
            run = self.get_run(agent_id, run_id)
            
            if on_status:
                on_status(run.status, attempt)
            
            if RunStatus.is_terminal(run.status):
                return run
            
            if attempt < max_attempts:
                time.sleep(interval_seconds)
        
        raise CursorAPIError(
            "timeout",
            f"Run did not complete after {max_attempts} attempts",
        )
    
    def list_artifacts(self, agent_id: str) -> list[dict]:
        """
        List artifacts produced by an agent.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#list-artifacts
        
        Args:
            agent_id: Agent ID
            
        Returns:
            List of artifact entries with path and metadata
        """
        response = self._request("GET", f"/agents/{agent_id}/artifacts")
        return response.get("items", [])
    
    def get_artifact_download_url(self, agent_id: str, artifact_path: str) -> str:
        """
        Get a presigned download URL for an artifact.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#download-an-artifact
        
        Args:
            agent_id: Agent ID
            artifact_path: Relative path from list_artifacts (e.g., "artifacts/file.png")
            
        Returns:
            Presigned download URL
        """
        from urllib.parse import quote
        
        response = self._request(
            "GET",
            f"/agents/{agent_id}/artifacts/download?path={quote(artifact_path)}",
        )
        return response.get("url", "")
