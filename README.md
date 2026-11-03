# roxy-vllm
This is code for a 'robust proxy' that is meant to supplement vllm when clients can't stream tokens when running in the background. This handles a 'handoff' that changes from streaming to 'batched' on the client side, while vllm is left uninterrupted, streaming the response.
