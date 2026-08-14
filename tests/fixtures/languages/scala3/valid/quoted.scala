import scala.quoted.*

object Quoted:
  val character = '\u03bb'
  def expression(using Quotes): Expr[Int] = '{ 1 + 2 }
