"""
Cursor Cloud Agents API Client

Official documentation: https://cursor.com/docs/cloud-agent/api/endpoints
API base URL: https://api.cursor.com/v1

This client provides a minimal interface to the Cursor Cloud Agents API
for the research pipeline orchestrator.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote, urljoin, urlsplit

# Type alias for HTTP client (injected for testing)
HttpClient = Callable[[str, str, Optional[dict], Optional[dict]], tuple[int, dict]]

# Fetches the bytes behind a URL, sending no credentials: (url) -> (status, bytes)
DownloadClient = Callable[[str], tuple[int, bytes]]


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


class TransportError(Exception):
    """A request got no HTTP response (connection failure, timeout).

    Whether the server received and acted on the request is unknown.
    """


class APIError(Exception):
    """An API request failed."""
    def __init__(self, code: str, message: str, status_code: int = 0):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"{code}: {message}")
    
    @property
    def is_transient(self) -> bool:
        """True for failures that may pass on a later try: no response, 429, or 5xx."""
        return (
            self.code == "network_error"
            or self.status_code == 429
            or self.status_code >= 500
        )


class CursorAPIError(APIError):
    """Error from Cursor API."""


class GitHubAPIError(APIError):
    """Error from the GitHub REST API."""


def _default_http_client(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> tuple[int, dict]:
    """Default HTTP client using requests library."""
    import requests
    
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            timeout=60,
        )
    except requests.RequestException as error:
        raise TransportError(f"{type(error).__name__}: {error}") from error
    
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    
    return response.status_code, body


def _default_download_client(url: str) -> tuple[int, bytes]:
    """Default download client. Sends no credentials to the URL's host."""
    import requests
    
    try:
        response = requests.get(url, timeout=300)
    except requests.RequestException as error:
        raise TransportError(f"{type(error).__name__}: {error}") from error
    return response.status_code, response.content


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
        download_client: Optional[DownloadClient] = None,
    ):
        """
        Initialize the API client.
        
        Args:
            api_key: Cursor API key. If not provided, reads from CURSOR_API_KEY env var.
            base_url: API base URL. Defaults to https://api.cursor.com/v1
            http_client: HTTP client function for testing. Defaults to requests-based client.
            download_client: Fetches artifact bytes from a presigned URL (for testing).
        """
        self.api_key = api_key or os.environ.get("CURSOR_API_KEY")
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._http = http_client or _default_http_client
        self._download = download_client or _default_download_client
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
        try:
            status_code, response = self._http(method, url, self._headers(), body)
        except TransportError as error:
            raise CursorAPIError(
                code="network_error",
                message=f"No response to {method} {endpoint}: {error}",
            ) from error
        
        if status_code >= 400:
            error = response.get("error", {}) if isinstance(response, dict) else {}
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
        agent_id: Optional[str] = None,
    ) -> AgentRun:
        """
        Create a cloud agent and start its initial run.
        
        This starts a paid agent. To send the same launch again safely, pass
        the same agent_id: the API then answers 409 agent_id_conflict instead
        of creating a second agent.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#create-an-agent
        
        Args:
            prompt_text: The instruction text for the agent
            repo_url: Repository URL (e.g., https://github.com/org/repo)
            starting_ref: Branch name or commit SHA to start from
            model_id: Model ID from GET /v1/models. Omit for configured default.
            model_params: Optional model parameters [{id, value}, ...]
            name: Display name for the agent (max 100 chars)
            auto_create_pr: Whether to auto-create PR on completion
            agent_id: Client-chosen agent ID in bc-<uuid> form (the API's `agentId`)
            
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
        
        if agent_id:
            body["agentId"] = agent_id
        
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
    
    def get_agent(self, agent_id: str) -> AgentRun:
        """
        Look up an agent by ID and return it with its latest run.
        
        Official docs: https://cursor.com/docs/cloud-agent/api/endpoints#get-an-agent
        
        Raises:
            CursorAPIError: with status_code 404 if no such agent exists.
        """
        response = self._request("GET", f"/agents/{agent_id}")
        run_id = response.get("latestRunId")
        if not run_id:
            raise CursorAPIError(
                code="no_run",
                message=f"Agent {agent_id} exists but reports no latestRunId",
            )
        return AgentRun(
            agent_id=agent_id,
            run_id=run_id,
            status="UNKNOWN",
            agent_url=response.get("url"),
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
            Presigned download URL (valid for a short time)
        """
        response = self._request(
            "GET",
            f"/agents/{agent_id}/artifacts/download?path={quote(artifact_path, safe='')}",
        )
        return response.get("url", "")
    
    def download_artifact(self, agent_id: str, artifact_path: str) -> bytes:
        """
        Download one artifact and return its bytes.
        
        Asks the API for a presigned URL, then fetches that URL without the
        Cursor API key, because the URL points at another host.
        
        Raises:
            CursorAPIError: if the URL is not https or the download fails.
        """
        url = self.get_artifact_download_url(agent_id=agent_id, artifact_path=artifact_path)
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise CursorAPIError(
                code="unsafe_download_url",
                message=f"The download URL for '{artifact_path}' is not an https URL",
            )
        
        try:
            status_code, content = self._download(url)
        except TransportError as error:
            raise CursorAPIError(
                code="network_error",
                message=f"No response when downloading '{artifact_path}': {error}",
            ) from error
        
        if status_code != 200:
            raise CursorAPIError(
                code="download_error",
                message=f"Downloading '{artifact_path}' returned HTTP {status_code}",
                status_code=status_code,
            )
        return content


GITHUB_REPO_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """Return (owner, repository) for a github.com repository URL.

    Raises:
        ValueError: if the URL is not a github.com repository URL.
    """
    match = GITHUB_REPO_URL_PATTERN.match(url.strip()) if isinstance(url, str) else None
    if not match:
        raise ValueError(f"Not a github.com repository URL: {url!r}")
    return match.group(1), match.group(2)


class GitHubRepoReader:
    """
    Read-only access to one GitHub repository through the GitHub REST API.
    
    Used to turn a branch into a commit SHA and to check that files exist
    at a commit. It never writes to the repository.
    
    Official docs:
      https://docs.github.com/en/rest/git/refs#get-a-reference
      https://docs.github.com/en/rest/git/commits#get-a-commit-object
      https://docs.github.com/en/rest/repos/contents#get-repository-content
    """
    
    API_BASE_URL = "https://api.github.com"
    
    def __init__(
        self,
        repo_url: str,
        token: Optional[str] = None,
        http_client: Optional[HttpClient] = None,
    ):
        """
        Args:
            repo_url: github.com repository URL.
            token: GitHub token, needed only for a private repository. If not
                provided, reads the GITHUB_TOKEN environment variable.
            http_client: HTTP client function for testing.
        """
        self.owner, self.repo = parse_github_repo_url(repo_url)
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self._http = http_client or _default_http_client
    
    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"
    
    def is_same_repo(self, repo_url: str) -> bool:
        """True if a URL (with or without scheme) names this repository."""
        try:
            owner, repo = parse_github_repo_url(repo_url)
        except ValueError:
            return False
        return (owner.lower(), repo.lower()) == (self.owner.lower(), self.repo.lower())
    
    def _get(self, endpoint: str) -> tuple[int, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.API_BASE_URL}/repos/{self.owner}/{self.repo}/{endpoint}"
        try:
            return self._http("GET", url, headers, None)
        except TransportError as error:
            raise GitHubAPIError(
                code="network_error",
                message=f"No response from GitHub for {endpoint}: {error}",
            ) from error
    
    def _error(self, status_code: int, body: Any, what: str) -> GitHubAPIError:
        detail = body.get("message", "") if isinstance(body, dict) else ""
        return GitHubAPIError(
            code="github_error",
            message=f"GitHub returned HTTP {status_code} for {what} in {self.full_name}: {detail}",
            status_code=status_code,
        )
    
    def get_branch_commit(self, branch: str) -> str:
        """Return the commit SHA a branch points at now."""
        status_code, body = self._get(f"git/ref/heads/{quote(branch, safe='/')}")
        if status_code != 200:
            raise self._error(status_code=status_code, body=body, what=f"branch '{branch}'")
        target = body.get("object", {}) if isinstance(body, dict) else {}
        if target.get("type") != "commit" or not target.get("sha"):
            raise GitHubAPIError(
                code="github_error",
                message=f"Branch '{branch}' in {self.full_name} does not point at a commit",
            )
        return target["sha"]
    
    def get_commit(self, commit: str) -> str:
        """Return the SHA of a commit after checking that it exists."""
        status_code, body = self._get(f"git/commits/{quote(commit, safe='')}")
        if status_code != 200 or not isinstance(body, dict) or not body.get("sha"):
            raise self._error(status_code=status_code, body=body, what=f"commit '{commit}'")
        return body["sha"]
    
    def file_exists(self, path: str, commit: str) -> bool:
        """True if a file (not a folder) exists at a path in a commit."""
        status_code, body = self._get(
            f"contents/{quote(path, safe='/')}?ref={quote(commit, safe='')}"
        )
        if status_code == 404:
            return False
        if status_code != 200:
            raise self._error(
                status_code=status_code, body=body, what=f"file '{path}' at {commit}",
            )
        return isinstance(body, dict) and body.get("type") == "file"
