import re
import uuid
from datetime import datetime
from functools import wraps

import cherrypy
import pytz
import six
from cherrypy import HTTPError
from dateutil import parser as dateparser
from pockets import unwrap
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import subqueryload

from uber.barcode import get_badge_num_from_barcode
from uber.config import c
from uber.decorators import department_id_adapter
from uber.errors import CSRFException
from uber.models import AdminAccount, ApiToken, Attendee, DeptMembership, DeptMembershipRequest, Job, \
    Session, Shift
from uber.server import register_jsonrpc
from uber.utils import check_csrf, normalize_newlines


__version__ = '0.1'


def docstring_format(*args, **kwargs):
    def _decorator(obj):
        obj.__doc__ = obj.__doc__.format(*args, **kwargs)
        return obj
    return _decorator


def _format_opts(opts):
    html = ['<table class="opts"><tbody>']
    for value, label in opts:
        html.append(
            '<tr class="opt">'
            '<td class="opt-value">{}</td>'
            '<td class="opt-label">{}</td>'
            '</tr>'.format(value, label))
    html.append('</tbody></table>')
    return ''.join(html)


def _attendee_fields_and_query(full, query):
    if full:
        fields = AttendeeLookup.fields_full
        query = query.options(
            subqueryload(Attendee.dept_memberships),
            subqueryload(Attendee.assigned_depts),
            subqueryload(Attendee.food_restrictions),
            subqueryload(Attendee.shifts).subqueryload(Shift.job))
    else:
        fields = AttendeeLookup.fields
        query = query.options(subqueryload(Attendee.dept_memberships))
    return (fields, query)


def _parse_datetime(d):
    if isinstance(d, six.string_types) and d.strip().lower() == 'now':
        d = datetime.now(pytz.UTC)
    else:
        d = dateparser.parse(d)
    try:
        d = d.astimezone(pytz.UTC)  # aware object can be in any timezone
    except ValueError:
        d = c.EVENT_TIMEZONE.localize(d)  # naive assumed to be event timezone
    return d


def auth_by_token(required_access):
    token = cherrypy.request.headers.get('X-Auth-Token', None)
    if not token:
        return (401, 'Missing X-Auth-Token header')

    try:
        token = uuid.UUID(token)
    except ValueError as ex:
        return (403, 'Invalid auth token, {}: {}'.format(ex, token))

    with Session() as session:
        api_token = session.query(ApiToken).filter_by(token=token).first()
        if not api_token:
            return (403, 'Auth token not recognized: {}'.format(token))
        if api_token.revoked_time:
            return (403, 'Revoked auth token: {}'.format(token))
        if not required_access.issubset(set(api_token.access_ints)):
            # If the API call requires extra access, like c.ADMIN, check if the
            # associated admin account has the required access.
            extra_required_access = required_access.difference(set(c.API_ACCESS.keys()))
            if not extra_required_access or not extra_required_access.issubset(api_token.admin_account.access_ints):
                return (403, 'Insufficient access for auth token: {}'.format(token))
        cherrypy.session['account_id'] = api_token.admin_account_id
    return None


def auth_by_session(required_access):
    try:
        check_csrf()
    except CSRFException as ex:
        return (403, 'Your CSRF token is invalid. Please go back and try again.')
    admin_account_id = cherrypy.session.get('account_id', None)
    if not admin_account_id:
        return (403, 'Missing admin account in session')
    with Session() as session:
        admin_account = session.query(AdminAccount).filter_by(id=admin_account_id).first()
        if not admin_account:
            return (403, 'Invalid admin account in session')
        if not required_access.issubset(set(admin_account.access_ints)):
            return (403, 'Insufficient access for admin account')
    return None


def api_auth(*required_access):
    required_access = set(required_access)

    def _decorator(fn):
        inner_func = unwrap(fn)
        if getattr(inner_func, 'required_access', None) is not None:
            return fn
        else:
            inner_func.required_access = required_access

        @wraps(fn)
        def _with_api_auth(*args, **kwargs):
            error = None
            for auth in [auth_by_token, auth_by_session]:
                result = auth(required_access)
                error = error or result
                if not result:
                    return fn(*args, **kwargs)
            raise HTTPError(*error)
        return _with_api_auth
    return _decorator


class all_api_auth:
    def __init__(self, *required_access):
        self.required_access = required_access

    def __call__(self, cls):
        for name, fn in cls.__dict__.items():
            if hasattr(fn, '__call__'):
                setattr(cls, name, api_auth(*self.required_access)(fn))
        return cls


@all_api_auth(c.API_READ)
class AttendeeLookup:
    fields = {
        'full_name': True,
        'first_name': True,
        'last_name': True,
        'legal_name': True,
        'email': True,
        'zip_code': True,
        'cellphone': True,
        'ec_name': True,
        'ec_phone': True,
        'checked_in': True,
        'badge_num': True,
        'badge_printed_name': True,
        'badge_status_label': True,
        'badge_type_label': True,
        'amount_unpaid': True,
        'donation_tier': True,
        'donation_tier_label': True,
        'donation_tier_paid': True,
        'staffing': True,
        'is_dept_head': True,
        'ribbon_labels': True,
    }

    fields_full = dict(fields, **{
        'assigned_depts_labels': True,
        'weighted_hours': True,
        'worked_hours': True,
        'food_restrictions': {
            'sandwich_pref_labels': True,
            'standard_labels': True,
            'freeform': True
        },
        'shifts': {
            'worked': True,
            'worked_label': True,
            'job': [
                'type_label', 'department_name', 'name', 'description',
                'start_time', 'end_time', 'extra15'
            ]
        }
    })

    def lookup(self, badge_num, full=False):
        """
        Returns a single attendee by badge number.

        Takes the badge number as the first parameter.

        Optionally, "full" may be passed as the second parameter to return the
        complete attendee record, including departments, shifts, and food
        restrictions.
        """
        with Session() as session:
            attendee_query = session.query(Attendee).filter_by(badge_num=badge_num)
            fields, attendee_query = _attendee_fields_and_query(full, attendee_query)
            attendee = attendee_query.first()
            if attendee:
                return attendee.to_dict(fields)
            else:
                return {'error': 'No attendee found with Badge #{}'.format(badge_num)}

    def search(self, query, full=False):
        """
        Searches for attendees using a freeform text query. Returns all
        matching attendees using the same search algorithm as the main
        attendee search box.

        Takes the search query as the first parameter.

        Optionally, "full" may be passed as the second parameter to return the
        complete attendee record, including departments, shifts, and food
        restrictions.
        """
        with Session() as session:
            attendee_query = session.search(query)
            fields, attendee_query = _attendee_fields_and_query(full, attendee_query)
            return [a.to_dict(fields) for a in attendee_query.limit(100)]

    def export(self, query, full=False):
        """
        Searches for attendees by either email or first and last name.

        `query` should be a comma or newline separated list of emails and
        "first last" name combos.

        Results are returned in the format expected by
        <a href="../import/staff">the staff importer</a>.
        """
        queries = [s.strip() for s in re.split('[\n,]', normalize_newlines(query)) if s.strip()]

        names = dict()
        emails = dict()
        ids = set()
        for q in queries:
            if '@' in q:
                emails[Attendee.normalize_email(q)] = q
            elif q:
                try:
                    ids.add(str(uuid.UUID(q)))
                except Exception:
                    first, _, last = [s.strip() for s in q.partition(' ')]
                    names[q] = (first.lower(), last.lower())

        with Session() as session:
            if full:
                options = [
                    subqueryload(Attendee.dept_memberships).subqueryload(DeptMembership.department),
                    subqueryload(Attendee.dept_membership_requests).subqueryload(DeptMembershipRequest.department)]
            else:
                options = []

            email_attendees = []
            if emails:
                email_attendees = session.query(Attendee).filter(Attendee.normalized_email.in_(list(emails.keys()))) \
                    .options(*options).order_by(Attendee.email, Attendee.id).all()

            known_emails = set(a.normalized_email for a in email_attendees)
            unknown_emails = sorted([email for normalized, email in emails.items() if normalized not in known_emails])

            name_attendees = []
            if names:
                filters = [
                    and_(func.lower(Attendee.first_name) == n[0], func.lower(Attendee.last_name) == n[1])
                    for n in names.values()]
                name_attendees = session.query(Attendee).filter(or_(*filters)) \
                    .options(*options).order_by(Attendee.email, Attendee.id).all()

            id_attendees = []
            if ids:
                id_attendees = session.query(Attendee).filter(Attendee.id.in_(ids)) \
                    .options(*options).order_by(Attendee.email, Attendee.id).all()

            known_names = set(a.full_name.lower() for a in name_attendees)
            unknown_names = sorted([full_name for full_name in names.keys() if full_name.lower() not in known_names])

            seen = set()
            all_attendees = [
                a for a in (id_attendees + email_attendees + name_attendees)
                if a.id not in seen and not seen.add(a.id)]

            fields = [
                'first_name',
                'last_name',
                'birthdate',
                'email',
                'zip_code',
                'birthdate',
                'international',
                'ec_name',
                'ec_phone',
                'cellphone',
                'badge_printed_name',
                'found_how',
                'comments',
                'admin_notes',
                'all_years',
            ]
            attendees = []
            for a in all_attendees:
                d = a.to_dict(fields)
                if full:
                    assigned_depts = {}
                    checklist_admin_depts = {}
                    dept_head_depts = {}
                    poc_depts = {}
                    for membership in a.dept_memberships:
                        assigned_depts[membership.department_id] = membership.department.name
                        if membership.is_checklist_admin:
                            checklist_admin_depts[membership.department_id] = membership.department.name
                        if membership.is_dept_head:
                            dept_head_depts[membership.department_id] = membership.department.name
                        if membership.is_poc:
                            poc_depts[membership.department_id] = membership.department.name

                    d.update({
                        'assigned_depts': assigned_depts,
                        'checklist_admin_depts': checklist_admin_depts,
                        'dept_head_depts': dept_head_depts,
                        'poc_depts': poc_depts,
                        'requested_depts': {
                            (m.department_id if m.department_id else 'All'):
                            (m.department.name if m.department_id else 'Anywhere')
                            for m in a.dept_membership_requests},
                    })
                attendees.append(d)

            return {
                'unknown_emails': unknown_emails,
                'unknown_names': unknown_names,
                'attendees': attendees,
            }


@all_api_auth(c.API_UPDATE)
class JobLookup:
    fields = {
        'name': True,
        'description': True,
        'department_name': True,
        'start_time': True,
        'end_time': True,
        'duration': True,
        'shifts': {
            'worked': True,
            'worked_label': True,
            'attendee': {
                'badge_num': True,
                'full_name': True,
                'first_name': True,
                'last_name': True,
                'email': True,
                'cellphone': True,
                'badge_printed_name': True
            }
        }
    }

    @department_id_adapter
    @api_auth(c.API_READ)
    def lookup(self, department_id, start_time=None, end_time=None):
        """
        Returns a list of all shifts for the given department.

        Takes the department id as the first parameter. For a list of all
        department ids call the "dept.list" method.

        Optionally, takes a "start_time" and "end_time" to constrain the
        results to a given date range. Dates may be given in any format
        supported by the
        <a href="http://dateutil.readthedocs.io/en/stable/parser.html">
        dateutil parser</a>, plus the string "now".

        Unless otherwise specified, "start_time" and "end_time" are assumed
        to be in the local timezone of the event.
        """
        with Session() as session:
            query = session.query(Job).filter_by(department_id=department_id)
            if start_time:
                start_time = _parse_datetime(start_time)
                query = query.filter(Job.start_time >= start_time)
            if end_time:
                end_time = _parse_datetime(end_time)
                query = query.filter(Job.start_time <= end_time)
            query = query.options(
                    subqueryload(Job.department),
                    subqueryload(Job.shifts).subqueryload(Shift.attendee))
            return [job.to_dict(self.fields) for job in query]

    def assign(self, job_id, attendee_id):
        """
        Assigns a shift for the given job to the given attendee.

        Takes the job id and attendee id as parameters.
        """
        with Session() as session:
            message = session.assign(attendee_id, job_id)
            if message:
                return {'error': message}
            else:
                session.commit()
                return session.job(job_id).to_dict(self.fields)

    def unassign(self, shift_id):
        """
        Unassigns whomever is working the given shift.

        Takes the shift id as the only parameter.
        """
        with Session() as session:
            try:
                shift = session.shift(shift_id)
                session.delete(shift)
                session.commit()
            except Exception:
                return {'error': 'Shift was already deleted'}
            else:
                return session.job(shift.job_id).to_dict(self.fields)

    @docstring_format(
        _format_opts(c.WORKED_STATUS_OPTS),
        _format_opts(c.RATING_OPTS))
    def set_worked(self, shift_id, status=c.SHIFT_WORKED, rating=c.UNRATED, comment=''):
        """
        Sets the given shift status as worked or not worked.

        Takes the shift id as the first parameter.

        Optionally takes the shift status, rating, and a comment required to
        explain either poor or excellent performance.

        <h6>Valid status values</h6>
        {}
        <h6>Valid rating values</h6>
        {}
        """
        try:
            status = int(status)
            assert c.WORKED_STATUS[status] is not None
        except Exception:
            return {'error': 'Invalid status: {}'.format(status)}

        try:
            rating = int(rating)
            assert c.RATINGS[rating] is not None
        except Exception:
            return {'error': 'Invalid rating: {}'.format(rating)}

        if rating in (c.RATED_BAD, c.RATED_GREAT) and not comment:
            return {'error': 'You must leave a comment explaining why the '
                    'staffer was rated as: {}'.format(c.RATINGS[rating])}

        with Session() as session:
            try:
                shift = session.shift(shift_id)
                shift.worked = status
                shift.rating = rating
                shift.comment = comment
                session.commit()
            except Exception:
                return {'error': 'Unexpected error setting status'}
            else:
                return session.job(shift.job_id).to_dict(self.fields)


@all_api_auth(c.API_READ)
class DepartmentLookup:
    def list(self):
        """
        Returns a list of department ids and names.
        """
        return c.DEPARTMENTS


@all_api_auth(c.API_READ)
class ConfigLookup:
    fields = [
        'EVENT_NAME',
        'ORGANIZATION_NAME',
        'YEAR',
        'EPOCH',
        'ESCHATON',
        'EVENT_VENUE',
        'EVENT_VENUE_ADDRESS',
        'AT_THE_CON',
        'POST_CON',
    ]

    def info(self):
        """
        Returns a list of all available configuration settings.
        """
        output = {field: getattr(c, field) for field in self.fields}
        output['API_VERSION'] = __version__
        return output

    def lookup(self, field):
        """
        Returns the given configuration setting. Takes the setting
        name as a single argument. For a list of available settings,
        call the "config.info" method.
        """
        if field.upper() in self.fields:
            return getattr(c, field.upper())


@all_api_auth(c.API_READ)
class BarcodeLookup:
    def lookup_attendee_from_barcode(self, barcode_value, full=False):
        """
        Returns a single attendee using the barcode value from their badge.

        Takes the (possibly encrypted) barcode value as the first parameter.

        Optionally, "full" may be passed as the second parameter to return the
        complete attendee record, including departments, shifts, and food
        restrictions.
        """
        badge_num = -1
        try:
            result = get_badge_num_from_barcode(barcode_value)
            badge_num = result['badge_num']
        except Exception as e:
            return {'error': "Couldn't look up barcode value: " + str(e)}

        # Note: A decrypted barcode can yield a valid badge num,
        # but that badge num may not be assigned to an attendee.
        with Session() as session:
            query = session.query(Attendee).filter_by(badge_num=badge_num)
            fields, query = _attendee_fields_and_query(full, query)
            attendee = query.first()
            if attendee:
                return attendee.to_dict(fields)
            else:
                return {'error': 'Valid barcode, but no attendee found with Badge #{}'.format(badge_num)}

    def lookup_badge_number_from_barcode(self, barcode_value):
        """
        Returns a badge number using the barcode value from the given badge.

        Takes the (possibly encrypted) barcode value as a single parameter.
        """
        try:
            result = get_badge_num_from_barcode(barcode_value)
            return {'badge_num': result['badge_num']}
        except Exception as e:
            return {'error': "Couldn't look up barcode value: " + str(e)}


if c.API_ENABLED:
    register_jsonrpc(AttendeeLookup(), 'attendee')
    register_jsonrpc(JobLookup(), 'shifts')
    register_jsonrpc(DepartmentLookup(), 'dept')
    register_jsonrpc(ConfigLookup(), 'config')
    register_jsonrpc(BarcodeLookup(), 'barcode')
