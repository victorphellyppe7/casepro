from __future__ import unicode_literals

import regex
import six

from abc import ABCMeta, abstractmethod
from casepro.backend import get_backend
from casepro.contacts.models import Group
from casepro.msgs.models import Label, Message
from casepro.utils import normalize
from collections import defaultdict
from enum import Enum


class Quantifier(Enum):
    """
    Tests are typically composed of multiple conditions, e.g. contains ANY of X, Y or Z.
    """
    NONE = 1
    ANY = 2
    ALL = 3

    @classmethod
    def from_json(cls, val):
        return cls[val.upper()]

    def to_json(self):
        return self.name.lower()

    def evaluate(self, condition_callables):
        if self == Quantifier.NONE:
            for condition in condition_callables:
                if condition():
                    return False
            return True
        elif self == Quantifier.ANY:
            for condition in condition_callables:
                if condition():
                    return True
            return False
        elif self == Quantifier.ALL:
            for condition in condition_callables:
                if not condition():
                    return False
            return True


class DeserializationContext(object):
    """
    Context object passed to all test or action from_json methods
    """
    def __init__(self, org):
        self.org = org


class Test(object):
    """
    A test which can be evaluated to true or false on a given message
    """
    __metaclass__ = ABCMeta

    CLASS_BY_TYPE = None  # lazily initialized below

    @classmethod
    def from_json(cls, json_obj, context):
        if not cls.CLASS_BY_TYPE:
            cls.CLASS_BY_TYPE = {
                ContainsTest.TYPE: ContainsTest,
                GroupsTest.TYPE: GroupsTest,
                FieldTest.TYPE: FieldTest,
            }

        test_type = json_obj['type']
        test_cls = cls.CLASS_BY_TYPE.get(test_type, None)
        if not test_cls:  # pragma: no cover
            raise ValueError("Unknown test type: %s" % test_type)

        return test_cls.from_json(json_obj, context)

    @abstractmethod
    def matches(self, message):
        """
        Subclasses must implement this to return a boolean.
        """


class ContainsTest(Test):
    """
    Test that returns whether the message text contains or doesn't contain the given keywords
    """
    TYPE = 'contains'

    def __init__(self, keywords, quantifier):
        self.keywords = [normalize(word) for word in keywords]
        self.quantifier = quantifier

    @classmethod
    def from_json(cls, json_obj, context):
        return cls(json_obj['keywords'], Quantifier.from_json(json_obj['quantifier']))

    def to_json(self):
        return {'type': self.TYPE, 'keywords': self.keywords, 'quantifier': self.quantifier.to_json()}

    def matches(self, message):
        text = normalize(message.text)

        def keyword_check(w):
            return lambda: bool(regex.search(r'\b' + w + r'\b', text, flags=regex.UNICODE | regex.V0))

        checks = [keyword_check(keyword) for keyword in self.keywords]

        return self.quantifier.evaluate(checks)


class GroupsTest(Test):
    """
    Test that returns whether the message was sent from the given contact groups
    """
    TYPE = 'groups'

    def __init__(self, groups, quantifier):
        self.groups = groups
        self.quantifier = quantifier

    @classmethod
    def from_json(cls, json_obj, context):
        groups = list(Group.objects.filter(org=context.org, uuid__in=json_obj['groups']).order_by('pk'))
        return cls(groups, Quantifier.from_json(json_obj['quantifier']))

    def to_json(self):
        return {'type': self.TYPE, 'groups': [g.uuid for g in self.groups], 'quantifier': self.quantifier.to_json()}

    def matches(self, message):
        contact_groups = set(message.contact.groups.all())

        def group_check(g):
            return lambda: g in contact_groups

        checks = [group_check(group) for group in self.groups]

        return self.quantifier.evaluate(checks)


class FieldTest(Test):
    """
    Test that returns whether the message was sent from a contact with the given field value
    """
    TYPE = 'field'

    def __init__(self, key, values):
        self.key = key
        self.values = [normalize(v) for v in values]

    @classmethod
    def from_json(cls, json_obj, context):
        return cls(json_obj['key'], json_obj['values'])

    def to_json(self):
        return {'type': self.TYPE, 'key': self.key, 'values': self.values}

    def matches(self, message):
        contact_value = normalize(message.contact.fields.get(self.key, ""))

        for value in self.values:
            if value == contact_value:
                return True
        return False


class Action(object):
    """
    An action which can be performed on a message
    """
    __metaclass__ = ABCMeta

    TYPE = None
    CLASS_BY_TYPE = None  # lazily initialized below

    @classmethod
    def from_json(cls, json_obj, context):
        if not cls.CLASS_BY_TYPE:
            cls.CLASS_BY_TYPE = {
                LabelAction.TYPE: LabelAction,
                FlagAction.TYPE: FlagAction,
                ArchiveAction.TYPE: ArchiveAction,
            }

        action_type = json_obj['type']
        action_cls = cls.CLASS_BY_TYPE.get(action_type)
        if not action_cls:  # pragma: no cover
            raise ValueError("Unknown action type: %s" % action_type)

        return action_cls.from_json(json_obj, context)

    def __eq__(self, other):
        return self.TYPE == other.TYPE

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.TYPE)


class LabelAction(Action):
    """
    Adds a label to the message
    """
    TYPE = 'label'

    def __init__(self, label):
        self.label = label

    @classmethod
    def from_json(cls, json_obj, context):
        return cls(Label.objects.get(org=context.org, uuid=json_obj['label']))

    def to_json(self):
        return {'type': self.TYPE, 'label': self.label.uuid}

    def apply_to(self, org, messages):
        for msg in messages:
            msg.labels.add(self.label)

        get_backend().label_messages(org, messages, self.label)

    def __eq__(self, other):
        return self.TYPE == other.TYPE and self.label == other.label

    def __hash__(self):
        return hash(self.TYPE + self.label.uuid)


class FlagAction(Action):
    """
    Flags the message
    """
    TYPE = 'flag'

    @classmethod
    def from_json(cls, json_obj, context):
        return cls()

    def to_json(self):
        return {'type': self.TYPE}

    def apply_to(self, org, messages):
        Message.objects.filter(pk__in=[m.pk for m in messages]).update(is_flagged=True)

        get_backend().flag_messages(org, messages)


class ArchiveAction(Action):
    """
    Archives the message
    """
    TYPE = 'archive'

    @classmethod
    def from_json(cls, json_obj, context):
        return cls()

    def to_json(self):
        return {'type': self.TYPE}

    def apply_to(self, org, messages):
        Message.objects.filter(pk__in=[m.pk for m in messages]).update(is_archived=True)

        get_backend().archive_messages(org, messages)


class Rule(object):
    """
    At some point this we'll likely separate rules from labels and this will become an actual model. For now we generate
    a rule for each label on the fly.
    """
    def __init__(self, tests, actions):
        self.tests = tests
        self.actions = actions

    @classmethod
    def from_label(cls, label):
        return cls(
            [ContainsTest(label.get_keywords(), Quantifier.ANY)],
            [LabelAction(label)]
        )

    def matches(self, message):
        """
        Returns whether this rule matches the given message, i.e. all of its tests match the message
        """
        for test in self.tests:
            if not test.matches(message):
                return False
        return True

    class BatchProcessor(object):
        """
        Applies a set of rules to a batch of messages in a way that allows same actions to be merged and reduces needed
        calls to the backend.
        """
        def __init__(self, org, rules):
            self.org = org
            self.rules = rules
            self.messages_by_action = defaultdict(set)

        def include_messages(self, *messages):
            """
            Includes the given messages in this batch processing
            :param messages: the messages to include
            :return: tuple of the number of rules matched, and the number of actions that will be performed
            """
            num_rules_matched = 0
            num_actions_deferred = 0

            for message in messages:
                for rule in self.rules:
                    if rule.matches(message):
                        num_rules_matched += 1
                        for action in rule.actions:
                            self.messages_by_action[action].add(message)
                            num_actions_deferred += 1

            return num_rules_matched, num_actions_deferred

        def apply_actions(self):
            """
            Applies the actions gathered by this processor
            """
            for action, messages in six.iteritems(self.messages_by_action):
                action.apply_to(self.org, messages)
