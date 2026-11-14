import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"customers", "orders", "products", "order_items"}

class UnsafeQuery(ValueError): pass

def validate_select(sql: str, max_rows: int = 200) -> str:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQuery("Could not parse generated SQL") from exc
    if len(statements) != 1 or statements[0] is None:
        raise UnsafeQuery("Only one SQL statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise UnsafeQuery("Only SELECT queries are allowed")
    if tree.args.get("into") or tree.args.get("locks"):
        raise UnsafeQuery("SELECT INTO and locking queries are not allowed")
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command)):
            raise UnsafeQuery("Only read-only SELECT queries are allowed")
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name.lower() not in ALLOWED_TABLES | cte_names or table.db or table.catalog:
            raise UnsafeQuery(f"Table is not available: {table.name}")
    if not tree.args.get("limit"):
        tree = tree.limit(max_rows)
    else:
        count = tree.args["limit"].expression
        try:
            if int(count.this) > max_rows:
                tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        except (AttributeError, ValueError, TypeError):
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return tree.sql(dialect="postgres")
