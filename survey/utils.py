# Copyright (c) 2023, DjaoDjin inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
This file contains functions useful throughout the whole project which depend
on importing Django models.

See helpers.py for functions useful throughout the whole project which do
not require to import `django` modules.
"""
import datetime, logging
from importlib import import_module

from django.apps import apps as django_apps
from django.conf import settings as django_settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count, F, FilteredRelation, Q
from django.http.request import split_domain_port, validate_host
from django.utils.timezone import utc

from . import settings
from .compat import import_string, urlparse, urlunparse
from .models import Answer, Unit
#pylint:disable=unused-import
from .queries import datetime_or_now, get_account_model, get_question_model

LOGGER = logging.getLogger(__name__)


def as_timestamp(dtime_at=None):
    if not dtime_at:
        dtime_at = datetime_or_now()
    return int((
        dtime_at - datetime.datetime(1970, 1, 1, tzinfo=utc)).total_seconds())


def get_accessible_accounts(grantees, campaign=None, aggregate_set=False,
                            start_at=None, ends_at=None):
    """
    All accounts which have elected to share samples with at least one
    account in grantees.
    """
    try:
        iter(grantees)
    except TypeError:
        grantees = [grantees]

    queryset = None
    if (hasattr(settings, 'ACCESSIBLE_ACCOUNTS_CALLABLE') and
        settings.ACCESSIBLE_ACCOUNTS_CALLABLE):
        queryset = import_string(settings.ACCESSIBLE_ACCOUNTS_CALLABLE)(
            grantees, campaign=campaign, aggregate_set=aggregate_set,
            start_at=start_at, ends_at=ends_at)

    if queryset is None:
        filter_params = {}
        if start_at:
            filter_params.update({
                'portfolio_double_optin_accounts__created_at__gte': start_at})
        if campaign:
            filter_params.update({'portfolios__campaign': campaign})
        # Implementation note: requires Django>=2 because of `FilteredRelation`
        # Adding the correct condition on the LEFT OUTER JOIN is quite
        # a challenge with Django. The SQL we need is as follow:
        #   SELECT DISTINCT saas_organization.*,
        #                 survey_portfoliodoubleoptin.extra AS _extra
        #   FROM saas_organization
        #   INNER JOIN survey_portfolio
        #   ON saas_organization.id = survey_portfolio.account_id
        #   LEFT OUTER JOIN survey_portfoliodoubleoptin
        #   ON saas_organization.id = survey_portfoliodoubleoptin.account_id
        #     AND survey_portfoliodoubleoptin.grantee_id IN (${grantees})
        #   WHERE survey_portfolio.grantee_id IN (${grantees});
        queryset = get_account_model().objects.filter(
            portfolios__grantee__in=grantees,
            **filter_params).annotate(
            granted=FilteredRelation(
                'portfolio_double_optin_accounts',
                condition=Q(
                    portfolio_double_optin_accounts__grantee__in=grantees
            ))).annotate(_extra=F('granted__extra'))

    return queryset


def get_benchmarks_enumerated(samples, questions, questions_by_key=None):
    """
    Returns a dictionnary indexed by a question's primary key where
    each question in `questions` is associated a a dictionnary that contains:
      - the total number of samples in `samples` with an anwer to the question
      - a dictionnary of the number of samples in `samples` for each choice
        available when the question's unit is an enum.

    Example:

    {
        12: {
            "path": "/sustainability/governance/formalized-esg-strategy",
            "nb_respondents": 10,
            "rate": {
                "Yes": 5,
                "No": 5
            }
        }
    }
    """
    if not questions_by_key:
        questions_by_key = {}

    # total number of answers
    for row in Answer.objects.filter(
            question__in=questions,
            unit_id=F('question__default_unit_id'),
            sample_id__in=samples).values('question__id',
                'question__path').annotate(Count('sample_id')):
        question_pk = row['question__id']
        count = row['sample_id__count']
        path = row['question__path']
        value = questions_by_key.get(question_pk, {'path': path})
        value.update({'nb_respondents': count})
        if question_pk not in questions_by_key:
            questions_by_key.update({question_pk: value})

    # per-choice number of answers
    enum_answers = Answer.objects.filter(
            question__in=questions,
            unit_id=F('question__default_unit_id'),
            sample_id__in=samples,
            question__default_unit__system__in=[Unit.SYSTEM_ENUMERATED,
                Unit.SYSTEM_DATETIME], # XXX target year are stored as choices
            unit__enums__id=F('measured')).values('question__id',
                'unit__enums__text').annotate(Count('sample_id'))
    for row in enum_answers:
        question_pk = row['question__id']
        count = row['sample_id__count']
        measured = row['unit__enums__text']
        value = questions_by_key.get(question_pk)
        total = value.get('nb_respondents', None)
        rate = value.get('rate', {})
        rate.update({
            measured: (int(count * 100 // total) if total else 0)})
        if 'rate' not in value:
            value.update({'rate': rate})
        if question_pk not in questions_by_key:
            questions_by_key.update({question_pk: value})

    return questions_by_key


def get_account_serializer():
    """
    Returns the ``AccountSerializer`` model that is active in this project.
    """
    path = settings.ACCOUNT_SERIALIZER
    dot_pos = path.rfind('.')
    module, attr = path[:dot_pos], path[dot_pos + 1:]
    try:
        mod = import_module(module)
    except (ImportError, ValueError) as err:
        raise ImproperlyConfigured(
            "Error importing class '%s' defined by ACCOUNT_SERIALIZER (%s)"
            % (path, err))
    try:
        cls = getattr(mod, attr)
    except AttributeError:
        raise ImproperlyConfigured('Module "%s" does not define a "%s"'\
' check the value of ACCOUNT_SERIALIZER' % (module, attr))
    return cls


def get_belongs_model():
    """
    Returns the ``Account`` model that owns campaigns and matrices.
    """
    try:
        return django_apps.get_model(settings.BELONGS_MODEL)
    except ValueError:
        raise ImproperlyConfigured(
            "BELONGS_MODEL must be of the form 'app_label.model_name'")
    except LookupError:
        raise ImproperlyConfigured("BELONGS_MODEL refers to model '%s'"\
" that has not been installed" % settings.BELONGS_MODEL)


def get_content_model():
    """
    Returns the ``Content`` model that is active in this project.
    """
    try:
        return django_apps.get_model(settings.CONTENT_MODEL)
    except ValueError:
        raise ImproperlyConfigured(
            "CONTENT_MODEL must be of the form 'app_label.model_name'")
    except LookupError:
        raise ImproperlyConfigured("CONTENT_MODEL refers to model '%s'"\
" that has not been installed" % settings.CONTENT_MODEL)


def get_question_serializer():
    """
    Returns the ``QuestionDetailSerializer`` model that is active
    in this project.
    """
    path = settings.QUESTION_SERIALIZER
    dot_pos = path.rfind('.')
    module, attr = path[:dot_pos], path[dot_pos + 1:]
    try:
        mod = import_module(module)
    except (ImportError, ValueError) as err:
        raise ImproperlyConfigured(
            "Error importing class '%s' defined by QUESTION_SERIALIZER (%s)"
            % (path, err))
    try:
        cls = getattr(mod, attr)
    except AttributeError:
        raise ImproperlyConfigured('Module "%s" does not define a "%s"'\
' check the value of QUESTION_SERIALIZER' % (module, attr))
    return cls


def get_user_serializer():
    """
    Returns the user serializer model that is active in this project.
    """
    return import_string(settings.USER_SERIALIZER)


def get_user_detail_serializer():
    """
    Returns the user serializer model that is active in this project.
    """
    return import_string(settings.USER_DETAIL_SERIALIZER)


def validate_redirect(request):
    """
    Get the REDIRECT_FIELD_NAME and validates it is a URL on allowed hosts.
    """
    return validate_redirect_url(request.GET.get(REDIRECT_FIELD_NAME, None))


def validate_redirect_url(next_url):
    """
    Returns the next_url path if next_url matches allowed hosts.
    """
    if not next_url:
        return None
    parts = urlparse(next_url)
    if parts.netloc:
        domain, _ = split_domain_port(parts.netloc)
        allowed_hosts = (['*'] if django_settings.DEBUG
            else django_settings.ALLOWED_HOSTS)
        if not (domain and validate_host(domain, allowed_hosts)):
            return None
    return urlunparse(("", "", parts.path,
        parts.params, parts.query, parts.fragment))
