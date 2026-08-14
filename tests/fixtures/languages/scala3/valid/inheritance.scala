trait Base
class Child extends Base
object Namespace:
  val marker = 1
object Use:
  import Namespace.*
  val item = new Child
  val inherited = marker
