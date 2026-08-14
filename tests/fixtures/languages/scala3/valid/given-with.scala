trait Codec[A]
given intCodec: Codec[Int] with
  def name = "int"
