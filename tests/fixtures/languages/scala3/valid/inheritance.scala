trait Base:
  val marker = 1
class Child extends Base
object Namespace:
  val imported = 2
object Use:
  import Namespace.*
  val item = new Child
  val inherited = item.marker
  val wildcard = imported
