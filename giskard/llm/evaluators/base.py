from typing import Dict, Optional, Sequence, Tuple

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ...core.test_result import TestResultDetails
from ...datasets.base import Dataset
from ...models.base.model import BaseModel
from ..client import LLMClient, get_default_client
from ..client.base import ChatMessage
from ..errors import LLMGenerationError
from .utils import format_conversation


@dataclass
class EvaluationResult:
    failure_examples: Sequence[dict]
    success_examples: Sequence[dict]
    errors: Sequence[dict]
    details: Optional[TestResultDetails] = None

    @property
    def passed(self):
        return len(self.failure_examples) == 0 and len(self.success_examples) > 0

    @property
    def failed(self):
        return not self.passed

    @property
    def has_errors(self):
        return len(self.errors) > 0

    @property
    def passed_ratio(self):
        return len(self.success_examples) / (len(self.success_examples) + len(self.failure_examples))

    def add_error(self, error: str, conversation: Sequence[Dict]):
        self.errors.append({"error": error, "conversation": conversation})

    def add_sample(self, eval_passed: bool, reason: str, conversation: Sequence[Dict]):
        # @TODO: improve this
        if eval_passed:
            self.success_examples.append({"conversation": conversation, "reason": reason})
        else:
            self.failure_examples.append({"conversation": conversation, "reason": reason})


class BaseEvaluator(ABC):
    """Base interface for evaluators."""

    @abstractmethod
    def evaluate(self, model: BaseModel, dataset: Dataset):
        ...


class _BaseLLMEvaluator(BaseEvaluator):
    @abstractmethod
    def _format_messages(self, model: BaseModel, conversation: Sequence[Dict]) -> Sequence[ChatMessage]:
        ...

    def evaluate(self, model: BaseModel, dataset: Dataset):
        model_outputs = model.predict(dataset).prediction

        result = EvaluationResult()
        for _, input_vars, model_output in zip(
            dataset.df.index,
            dataset.df.loc[:, model.feature_names].to_dict("records"),
            model_outputs,
        ):
            conversation = [{"role": "user", "content": input_vars}, {"role": "agent", "content": model_output}]

            messages = self._format_messages(model, conversation)
            try:
                raw_eval = self.llm_client.complete(
                    messages,
                    temperature=self.llm_temperature,
                    caller_id=self.__class__.__name__,
                    seed=self.llm_seed,
                )
                eval_passed, reason = self._parse_evaluation(raw_eval)
            except LLMGenerationError as err:
                result.add_error(str(err), conversation)
                continue

            result.add_eval(eval_passed, reason, conversation)

        return result

    def _parse_evaluation_output(self, raw_eval: ChatMessage) -> Tuple[bool, str]:
        try:
            eval_result = json.loads(raw_eval.content)
            return eval_result["eval_passed"], eval_result.get("reason")
        except (AttributeError, KeyError) as err:
            raise LLMGenerationError("Could not parse evaluator output") from err


class LLMBasedEvaluator(_BaseLLMEvaluator):
    def __init__(
        self,
        prompt: str,
        prefix_messages: Optional[Sequence[ChatMessage]] = None,
        llm_temperature=0.1,
        llm_client: LLMClient = None,
        llm_seed: int = 1729,
    ):
        self.prompt = prompt
        self.prefix_messages = prefix_messages or []
        self.llm_temperature = llm_temperature
        self.llm_client = llm_client if llm_client is not None else get_default_client()
        self.llm_seed = llm_seed

    def _format_messages(self, model: BaseModel, conversation: Sequence[Dict]) -> Sequence[ChatMessage]:
        prompt = self._prompt.format(model=model, conversation=format_conversation(conversation))
        return self._messages + [ChatMessage(role="user", content=prompt)]
