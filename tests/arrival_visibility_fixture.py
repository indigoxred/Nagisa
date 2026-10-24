import tempfile
import unittest
import os
import copy
from pathlib import Path
from nagisa.reconciler.arrival_identity import VerifiedArrival
from nagisa.reconciler.arrival_visibility import PlexArrivalGate


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);source=root/'episode.mkv';source.write_bytes(b'video')
        self.root=root
        stat=source.stat()
        self.verified=VerifiedArrival(123,99,1044,2200,source.resolve(),
                                      (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
        self.item={'ratingKey':'1','type':'episode','guid':'tv.plex.agents.custom.shoko://episode/e2200','grandparentGuid':'tv.plex.agents.custom.shoko://show/1044','Media':[{'Part':[{'file':str(source),'size':5}]}]}
        self.sections={53:[dict(self.item,librarySectionID=53)],55:[dict(self.item,librarySectionID=55)]}
        self.gate=PlexArrivalGate((53,55),(root,))

    def test_exact_source_and_identity_in_both_libraries(self):
        self.assertTrue(self.gate.visible(self.verified,self.sections))

    def test_one_configured_library_requires_one_exact_card(self):
        gate=PlexArrivalGate((53,),(self.root,))
        self.assertTrue(gate.visible(self.verified,{53:self.sections[53]}))
        self.assertFalse(gate.visible(self.verified,{53:[]}))
        duplicate=copy.deepcopy(self.sections[53][0])
        duplicate['ratingKey']='2'
        self.assertFalse(gate.visible(
            self.verified,{53:[self.sections[53][0],duplicate]}))

    def test_one_configured_library_checks_every_media_part(self):
        gate=PlexArrivalGate((53,),(self.root,))
        exact=copy.deepcopy(self.sections[53][0])
        exact['Media'].append({'Part':[{'file':str(self.verified.source),
                                        'size':5}]})
        self.assertTrue(gate.visible(self.verified,{53:[exact]}))
        other=self.root/'other.mkv';other.write_bytes(b'video')
        exact['Media'][1]['Part'][0]['file']=str(other)
        self.assertFalse(gate.visible(self.verified,{53:[exact]}))

    def test_section_configuration_must_be_nonempty_unique_and_positive(self):
        for sections in ((),(53,53),(0,),(-1,),('53',),(True,)):
            with self.subTest(sections=sections):
                with self.assertRaises(ValueError):
                    PlexArrivalGate(sections,(self.root,))

    def test_bind_mount_alias_of_exact_source_is_visible(self):
        presentation=self.root/'presentation';presentation.mkdir()
        alias=presentation/'episode.mkv'
        os.link(self.verified.source,alias)
        for items in self.sections.values():
            items[0]['Media'][0]['Part'][0]['file']=str(alias)
        self.gate=PlexArrivalGate((53,55),(presentation,))
        self.assertNotEqual(alias.resolve(),self.verified.source)
        self.assertTrue(self.gate.visible(self.verified,self.sections))

    def test_same_size_different_file_is_not_visible(self):
        presentation=self.root/'presentation';presentation.mkdir()
        other=presentation/'episode.mkv';other.write_bytes(b'video')
        for items in self.sections.values():
            items[0]['Media'][0]['Part'][0]['file']=str(other)
        self.gate=PlexArrivalGate((53,55),(presentation,))
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_card_with_arrival_and_unrelated_part_is_not_delivery(self):
        other=self.root/'unrelated.mkv';other.write_bytes(b'other')
        for section in (53,55):
            self.sections[section][0]=copy.deepcopy(self.sections[section][0])
            self.sections[section][0]['Media'][0]['Part'].append(
                {'file':str(other),'size':5})
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_one_library_is_not_delivery(self):
        self.sections[55]=[]
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_wrong_episode_guid_is_not_delivery(self):
        self.sections[55][0]['guid']='tv.plex.agents.custom.shoko://episode/e2201'
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_trashed_episode_is_not_delivery(self):
        self.sections[55][0]['deletedAt']=1234
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_wrong_declared_library_is_not_delivery(self):
        self.sections[55][0]['librarySectionID']=53
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_duplicate_active_episode_objects_require_review(self):
        self.sections[55].append(dict(self.sections[55][0],ratingKey='2'))
        self.assertFalse(self.gate.visible(self.verified,self.sections))

    def test_repeated_active_object_with_same_key_requires_review(self):
        self.sections[55].append(copy.deepcopy(self.sections[55][0]))
        self.assertFalse(self.gate.visible(self.verified,self.sections))
