"""Define self-attention decoder."""

import tensorflow as tf

from opennmt.decoders import decoder
from opennmt.layers import common, transformer
from opennmt.layers.position import SinusoidalPositionEncoder


class SelfAttentionDecoder(decoder.Decoder):
  """Encoder using self-attention as described in
  https://arxiv.org/abs/1706.03762.
  """

  def __init__(self,
               num_layers,
               num_units=512,
               num_heads=8,
               ffn_inner_dim=2048,
               dropout=0.1,
               attention_dropout=0.1,
               ffn_dropout=0.1,
               ffn_activation=tf.nn.relu,
               position_encoder_class=SinusoidalPositionEncoder,
               num_sources=1,
               **kwargs):
    """Initializes the parameters of the decoder.

    Args:
      num_layers: The number of layers.
      num_units: The number of hidden units.
      num_heads: The number of heads in the multi-head attention.
      ffn_inner_dim: The number of units of the inner linear transformation
        in the feed forward layer.
      dropout: The probability to drop units from the outputs.
      attention_dropout: The probability to drop units from the attention.
      ffn_dropout: The probability to drop units from the activation output in
        the feed forward layer.
      ffn_activation: The activation function to apply between the two linear
        transformations of the feed forward layer.
      position_encoder: The :class:`opennmt.layers.PositionEncoder`
        class to use for position encoding (or a callable that returns such
        class).
      num_sources: The number of source contexts expected by this decoder.
      **kwargs: Additional layer arguments.
    """
    super(SelfAttentionDecoder, self).__init__(num_sources=num_sources, **kwargs)
    self.num_units = num_units
    self.num_heads = num_heads
    self.dropout = dropout
    self.position_encoder = None
    if position_encoder_class is not None:
      self.position_encoder = position_encoder_class()
    self.layer_norm = common.LayerNorm()
    self.layers = [
        _SelfAttentionDecoderLayer(
            self.num_units,
            self.num_heads,
            ffn_inner_dim,
            num_sources=num_sources,
            dropout=dropout,
            attention_dropout=attention_dropout,
            ffn_dropout=ffn_dropout,
            ffn_activation=ffn_activation)
        for i in range(num_layers)]

  @property
  def minimum_sources(self):
    return 0

  @property
  def maximum_sources(self):
    return 1e6  # An arbitrary large number.

  @property
  def support_alignment_history(self):
    return self.num_sources == 1

  def _run(self,
           inputs,
           sequence_length=None,
           cache=None,
           memory=None,
           memory_sequence_length=None,
           step=None,
           training=None):
    # Process inputs.
    inputs *= self.num_units**0.5
    if self.position_encoder is not None:
      inputs = self.position_encoder(inputs, position=step + 1 if step is not None else None)
    inputs = common.dropout(inputs, self.dropout, training=training)

    # Prepare query mask.
    mask = None
    if sequence_length is not None:
      mask = transformer.future_mask(
          sequence_length, maximum_length=tf.shape(inputs)[1])

    # Prepare memory mask.
    memory_mask = None
    if memory is not None:
      if not isinstance(memory, (list, tuple)):
        memory = (memory,)
    if memory_sequence_length is not None:
      if not isinstance(memory_sequence_length, (list, tuple)):
        memory_sequence_length = (memory_sequence_length,)
      memory_mask = []
      for mem, mem_length in zip(memory, memory_sequence_length):
        mem_mask = tf.sequence_mask(mem_length, maxlen=tf.shape(mem)[1], dtype=tf.float32)
        mem_mask = tf.expand_dims(mem_mask, 1)
        memory_mask.append(mem_mask)

    # Run each layer.
    new_cache = []
    for i, layer in enumerate(self.layers):
      inputs, layer_cache, attention = layer(
          inputs,
          mask=mask,
          memory=memory,
          memory_mask=memory_mask,
          cache=cache[i] if cache is not None else None,
          training=training)
      new_cache.append(layer_cache)
    outputs = self.layer_norm(inputs)
    return outputs, new_cache, attention

  def forward(self,
              inputs,
              sequence_length=None,
              initial_state=None,
              memory=None,
              memory_sequence_length=None,
              input_fn=None,
              sampling_probability=None,
              training=None):
    _ = initial_state
    _ = input_fn
    if sampling_probability is not None:
      raise ValueError("Scheduled sampling is not supported by this decoder")
    outputs, state, attention = self._run(
        inputs,
        sequence_length=sequence_length,
        memory=memory,
        memory_sequence_length=memory_sequence_length,
        training=training)
    logits = self.output_layer(outputs)
    return logits, state, attention

  def step(self,
           inputs,
           timestep,
           state=None,
           memory=None,
           memory_sequence_length=None,
           training=None):
    inputs = tf.expand_dims(inputs, 1)
    outputs, state, attention = self._run(
        inputs,
        cache=state,
        memory=memory,
        memory_sequence_length=memory_sequence_length,
        step=timestep,
        training=training)
    outputs = tf.squeeze(outputs, axis=1)
    if attention is not None:
      attention = tf.squeeze(attention, axis=1)
    return outputs, state, attention

  def _get_initial_state(self, batch_size, dtype, initial_state=None):
    # The decoder state contains the keys and values projections of the previous timesteps.
    _ = initial_state
    cache = []
    for _ in self.layers:
      shape = [batch_size, self.num_heads, 0, self.num_units // self.num_heads]
      self_kv = (tf.zeros(shape, dtype=dtype), tf.zeros(shape, dtype=dtype))
      memory_kv = [
          (tf.zeros(shape, dtype=dtype), tf.zeros(shape, dtype=dtype))
          for _ in range(self.num_sources)]
      cache.append(dict(self_kv=self_kv, memory_kv=memory_kv))
    return cache


class _SelfAttentionDecoderLayer(tf.keras.layers.Layer):
  """Implements one self-attention decoding layer."""

  def __init__(self,
               num_units,
               num_heads,
               ffn_inner_dim,
               num_sources=1,
               dropout=0.1,
               attention_dropout=0.1,
               ffn_dropout=0.1,
               ffn_activation=tf.nn.relu,
               **kwargs):
    """Initializes the layer.

    Args:
      num_units: The number of hidden units.
      num_heads: The number of heads in the multi-head attention.
      ffn_inner_dim: The number of units of the inner linear transformation
        in the feed forward layer.
      num_sources: The number of source contexts.
      dropout: The probability to drop units from the outputs.
      attention_dropout: The probability to drop units from the attention.
      ffn_dropout: The probability to drop units from the activation output in
        the feed forward layer.
      ffn_activation: The activation function to apply between the two linear
        transformations of the feed forward layer.
      **kwargs: Additional layer arguments.
    """
    super(_SelfAttentionDecoderLayer, self).__init__(**kwargs)
    self.self_attention = transformer.MultiHeadAttention(
        num_heads,
        num_units,
        dropout=attention_dropout)
    self.self_attention = transformer.TransformerLayerWrapper(
        self.self_attention, dropout)
    self.attention = []
    for _ in range(num_sources):
      attention = transformer.MultiHeadAttention(
          num_heads,
          num_units,
          dropout=attention_dropout,
          return_attention=num_sources == 1)
      attention = transformer.TransformerLayerWrapper(
          attention, dropout)
      self.attention.append(attention)
    self.ffn = transformer.FeedForwardNetwork(
        ffn_inner_dim,
        num_units,
        dropout=ffn_dropout,
        activation=ffn_activation)
    self.ffn = transformer.TransformerLayerWrapper(
        self.ffn, dropout)

  # pylint: disable=arguments-differ
  def call(self,
           inputs,
           mask=None,
           memory=None,
           memory_mask=None,
           cache=None,
           training=None):
    """Runs the decoder layer."""
    if cache is None:
      cache = {}

    outputs, self_kv = self.self_attention(
        inputs,
        mask=mask,
        cache=cache.get("self_kv"),
        training=training)

    attention = None
    memory_kv = []
    if memory is not None:
      memory_cache = cache.get("memory_kv")
      if memory_cache is None:
        memory_cache = [None] * len(self.attention)
      for layer, mem, mem_mask, mem_cache in zip(
          self.attention, memory, memory_mask, memory_cache):
        result = layer(
            outputs,
            memory=mem,
            mask=mem_mask,
            cache=mem_cache,
            training=training)
        if len(result) == 3:
          outputs, memory_kv_i, attention = result
          attention = attention[:, 0]  # Use the first head for the attention vector.
        else:
          outputs, memory_kv_i = result
        memory_kv.append(memory_kv_i)

    outputs = self.ffn(outputs, training=training)
    cache = dict(self_kv=self_kv, memory_kv=memory_kv)
    return outputs, cache, attention
