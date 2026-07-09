from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    # parameters: JSON Schema "properties" dict (maps param name -> schema object)
    parameters: dict[str, dict]
    # required: list of required parameter names
    required: list[str]
    fn: Callable[..., str] = field(repr=False)

    def __call__(self, **kwargs: Any) -> str:
        return self.fn(**kwargs)

    def to_openai_schema(self) -> dict:
        """Return the OpenAI function-calling schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }
