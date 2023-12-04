# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx

from promptflow._constants import LINE_TIMEOUT_SEC
from promptflow._utils.logger_utils import bulk_logger
from promptflow.batch._errors import ExecutorServiceUnhealthy
from promptflow.contracts.run_info import FlowRunInfo
from promptflow.executor._result import AggregationResult, LineResult
from promptflow.storage._run_storage import AbstractRunStorage

EXECUTOR_UNHEALTHY_MESSAGE = "The executor service is currently not in a healthy state"


class AbstractExecutorProxy:
    @classmethod
    def create(
        cls,
        flow_file: Path,
        working_dir: Optional[Path] = None,
        *,
        connections: Optional[dict] = None,
        storage: Optional[AbstractRunStorage] = None,
        **kwargs,
    ) -> "AbstractExecutorProxy":
        """Create a new executor"""
        raise NotImplementedError()

    def destroy(self):
        """Destroy the executor"""
        pass

    async def exec_line_async(
        self,
        inputs: Mapping[str, Any],
        index: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> LineResult:
        """Execute a line"""
        raise NotImplementedError()

    async def exec_aggregation_async(
        self,
        batch_inputs: Mapping[str, Any],
        aggregation_inputs: Mapping[str, Any],
        run_id: Optional[str] = None,
    ) -> AggregationResult:
        """Execute aggregation nodes"""
        raise NotImplementedError()


class APIBasedExecutorProxy(AbstractExecutorProxy):
    @property
    def api_endpoint(self) -> str:
        """The basic API endpoint of the executor service.

        The executor proxy calls the executor service to get the
        line results and aggregation result through this endpoint.
        """
        raise NotImplementedError()

    async def exec_line_async(
        self,
        inputs: Mapping[str, Any],
        index: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> LineResult:
        start_time = datetime.utcnow()
        # ensure service health
        await self._ensure_executor_health()
        # call execution api to get line results
        url = self.api_endpoint + "/Execution"
        payload = {"run_id": run_id, "line_number": index, "inputs": inputs}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=LINE_TIMEOUT_SEC)
        # process the response
        response, has_error = self._process_http_response(response)
        if has_error:
            run_info = FlowRunInfo.create_with_error(start_time, inputs, index, run_id, response)
            return LineResult(output={}, aggregation_inputs={}, run_info=run_info, node_run_infos={})
        return LineResult.deserialize(response)

    async def exec_aggregation_async(
        self,
        batch_inputs: Mapping[str, Any],
        aggregation_inputs: Mapping[str, Any],
        run_id: Optional[str] = None,
    ) -> AggregationResult:
        # ensure service health
        await self._ensure_executor_health()
        # call aggregation api to get aggregation result
        async with httpx.AsyncClient() as client:
            url = self.api_endpoint + "/Aggregation"
            payload = {"run_id": run_id, "batch_inputs": batch_inputs, "aggregation_inputs": aggregation_inputs}
            response = await client.post(url, json=payload, timeout=LINE_TIMEOUT_SEC)
        response, _ = self._process_http_response(response)
        return AggregationResult.deserialize(response)

    def _process_http_response(self, response: httpx.Response):
        """Process the http response from the executor service

        :param response: the http response from the executor service
        :type response: httpx.Response
        :return: the response json and a bool value indicating whether there is an error in the line execution
        :rtype: dict, bool
        """
        status_code = response.status_code
        if status_code == 200:
            return response.json(), False
        else:
            bulk_logger.error(f"Error when calling executor API, status code: {status_code}, error: {response.text}")
            try:
                error_response = response.json()
                if "error" in error_response:
                    return error_response["error"], True
                return error_response, True
            except JSONDecodeError:
                # TODO: add more error handling
                return response.text, True

    async def _ensure_executor_health(self):
        """Ensure the executor service is healthy before calling the API to get the results

        During testing, we observed that the executor service started quickly on Windows.
        However, there is a noticeable delay in booting on Linux.

        So we set a specific waiting period. If the executor service fails to return to normal
        within the allocated timeout, an exception is thrown to indicate a potential problem.
        """
        waiting_health_timeout = 5
        start_time = datetime.utcnow()
        while (datetime.utcnow() - start_time).seconds < waiting_health_timeout:
            if await self.check_health():
                return
        raise ExecutorServiceUnhealthy(f"{EXECUTOR_UNHEALTHY_MESSAGE}. Please resubmit your flow and try again.")

    async def check_health(self):
        try:
            health_url = self.api_endpoint + "/health"
            async with httpx.AsyncClient() as client:
                response = await client.get(health_url)
            if response.status_code != 200:
                bulk_logger.warning(f"{EXECUTOR_UNHEALTHY_MESSAGE}. Response: {response.status_code} - {response.text}")
                return False
            return True
        except Exception as e:
            bulk_logger.warning(f"{EXECUTOR_UNHEALTHY_MESSAGE}. Error: {str(e)}")
            return False
