"""Server-side access scoping for monthly shift calendars.

Operators may only receive assignments belonging to their linked employee ID.
Admin-only staffing previews are never loaded for operators.
"""
import json


def calendar_assignments(connection, month, role, employee_id):
    sql = """SELECT a.*,e.name,p.name pattern_name,p.start_time,p.end_time
        FROM assignments a
        JOIN employees e ON e.id=a.employee_id
        LEFT JOIN shift_patterns p ON p.id=a.pattern_id
        WHERE substr(a.work_date,1,7)=?"""
    arguments = [month]
    if role != 'admin':
        # Unlinked accounts must not fall back to the team-wide calendar.
        if employee_id is None:
            return []
        sql += ' AND a.employee_id=?'
        arguments.append(employee_id)
    sql += ' ORDER BY a.work_date,e.name,a.id'
    return connection.execute(sql, tuple(arguments)).fetchall()


def admin_day_previews(connection, month, role):
    if role != 'admin':
        return {}
    rows = connection.execute(
        'SELECT work_date,plan_json FROM daily_skill_plans WHERE substr(work_date,1,7)=?',
        (month,),
    ).fetchall()
    return {row['work_date']: json.loads(row['plan_json']) for row in rows}
