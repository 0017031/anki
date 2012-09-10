# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki import Collection
from anki.utils import intTime
from anki.importing.base import Importer
from anki.lang import _
from anki.lang import ngettext

#
# Import a .anki2 file into the current collection. Used for migration from
# 1.x, shared decks, and import from a packaged deck.
#
# We can't rely on internal ids, so we:
# - compare notes by guid
# - compare models by schema signature
# - compare cards by note guid + ordinal
# - compare decks by name
#

class Anki2Importer(Importer):

    needMapper = False
    deckPrefix = None
    allowUpdate = True

    def run(self, media=None):
        self._prepareFiles()
        if media is not None:
            # Anki1 importer has provided us with a custom media folder
            self.src.media._dir = media
        try:
            self._import()
        finally:
            self.src.close(save=False)

    def _prepareFiles(self):
        self.dst = self.col
        self.src = Collection(self.file)

    def _import(self):
        self._decks = {}
        if self.deckPrefix:
            id = self.dst.decks.id(self.deckPrefix)
            self.dst.decks.select(id)
        self._prepareTS()
        self._prepareModels()
        self._importNotes()
        self._importCards()
        self._importMedia()
        self._postImport()
        self.dst.db.execute("vacuum")
        self.dst.db.execute("analyze")

    # Notes
    ######################################################################
    # - should note new for wizard

    def _importNotes(self):
        # build guid -> (id,mod,mid) hash
        self._notes = {}
        existing = {}
        for id, guid, mod, mid in self.dst.db.execute(
            "select id, guid, mod, mid from notes"):
            self._notes[guid] = (id, mod, mid)
            existing[id] = True
        # iterate over source collection
        add = []
        dirty = []
        usn = self.dst.usn()
        for note in self.src.db.execute(
            "select * from notes"):
            # turn the db result into a mutable list
            note = list(note)
            guid, mid = note[1:3]
            # missing from local col?
            if guid not in self._notes:
                # get corresponding local model
                lmid = self._mid(mid)
                # ensure id is unique
                while note[0] in existing:
                    note[0] += 999
                existing[note[0]] = True
                # rewrite internal ids, models, etc
                note[2] = lmid
                note[4] = usn
                add.append(note)
                dirty.append(note[0])
                # note we have the added note
                self._notes[guid] = (note[0], note[3], note[2])
            else:
                pass
                ## update existing note - not yet tested; for post 2.0
                # newer = note[3] > mod
                # if self.allowUpdate and self._mid(mid) == mid and newer:
                #     localNid = self._notes[guid][0]
                #     note[0] = localNid
                #     note[4] = usn
                #     add.append(note)
                #     dirty.append(note[0])
        # add to col
        self.dst.db.executemany(
            "insert or replace into notes values (?,?,?,?,?,?,?,?,?,?,?)",
            add)
        self.dst.updateFieldCache(dirty)
        self.dst.tags.registerNotes(dirty)

    # Models
    ######################################################################
    # Models in the two decks may share an ID but not a schema, so we need to
    # compare the field & template signature rather than just rely on ID. If
    # we created a new model on a conflict then multiple imports would end up
    # with lots of models however, so we store a list of "alternate versions"
    # of a model in the model, so that importing a model is idempotent.

    def _prepareModels(self):
        "Prepare index of schema hashes."
        self._modelMap = {}

    def _mid(self, mid):
        "Return local id for remote MID."
        # already processed this mid?
        if mid in self._modelMap:
            return self._modelMap[mid]
        src = self.src.models.get(mid).copy()
        # if it doesn't exist, we'll copy it over, preserving id
        if not self.dst.models.have(mid):
            self.dst.models.update(src)
            # if we're importing with a prefix, make the model default to it
            if self.deckPrefix:
                src['did'] = self.dst.decks.current()['id']
                # and give it a unique name if it's not a shared deck
                if self.deckPrefix != "shared":
                    src['name'] += " (%s)" % self.deckPrefix
            # make sure to bump usn
            self.dst.models.save(src)
            self._modelMap[mid] = mid
            return mid
        # if it does exist, do the schema match?
        dst = self.dst.models.get(mid)
        shash = self.src.models.scmhash(src)
        dhash = self.src.models.scmhash(dst)
        if shash == dhash:
            # reuse without modification
            self._modelMap[mid] = mid
            return mid
        # try any alternative versions
        vers = dst.get("vers")
        for v in vers:
            m = self.dst.models.get(v)
            if self.dst.models.scmhash(m) == shash:
                # valid alternate found; use that
                self._modelMap[mid] = m['id']
                return m['id']
        # need to add a new alternate version, with new id
        self.dst.models.add(src)
        if vers:
            dst['vers'].append(src['id'])
        else:
            dst['vers'] = [src['id']]
        self.dst.models.save(dst)
        return src['id']

    # Decks
    ######################################################################

    def _did(self, did):
        "Given did in src col, return local id."
        # already converted?
        if did in self._decks:
            return self._decks[did]
        # get the name in src
        g = self.src.decks.get(did)
        name = g['name']
        # if there's a prefix, replace the top level deck
        if self.deckPrefix:
            tmpname = "::".join(name.split("::")[1:])
            name = self.deckPrefix
            if tmpname:
                name += "::" + name
        # create in local
        newid = self.dst.decks.id(name)
        # pull conf over
        if 'conf' in g and g['conf'] != 1:
            self.dst.decks.updateConf(self.src.decks.getConf(g['conf']))
            g2 = self.dst.decks.get(newid)
            g2['conf'] = g['conf']
            self.dst.decks.save(g2)
        # save desc
        deck = self.dst.decks.get(newid)
        deck['desc'] = g['desc']
        self.dst.decks.save(deck)
        # add to deck map and return
        self._decks[did] = newid
        return newid

    # Cards
    ######################################################################

    def _importCards(self):
        # build map of (guid, ord) -> cid and used id cache
        self._cards = {}
        existing = {}
        for guid, ord, cid in self.dst.db.execute(
            "select f.guid, c.ord, c.id from cards c, notes f "
            "where c.nid = f.id"):
            existing[cid] = True
            self._cards[(guid, ord)] = cid
        # loop through src
        cards = []
        revlog = []
        cnt = 0
        usn = self.dst.usn()
        aheadBy = self.src.sched.today - self.dst.sched.today
        for card in self.src.db.execute(
            "select f.guid, f.mid, c.* from cards c, notes f "
            "where c.nid = f.id"):
            guid = card[0]
            # does the card's note exist in dst col?
            if guid not in self._notes:
                continue
            dnid = self._notes[guid]
            # does the card already exist in the dst col?
            ord = card[5]
            if (guid, ord) in self._cards:
                # fixme: in future, could update if newer mod time
                continue
            # doesn't exist. strip off note info, and save src id for later
            card = list(card[2:])
            scid = card[0]
            # ensure the card id is unique
            while card[0] in existing:
                card[0] += 999
            existing[card[0]] = True
            # update cid, nid, etc
            card[1] = self._notes[guid][0]
            card[2] = self._did(card[2])
            card[4] = intTime()
            card[5] = usn
            # review cards have a due date relative to collection
            if card[7] in (2, 3):
                card[8] -= aheadBy
            cards.append(card)
            # we need to import revlog, rewriting card ids and bumping usn
            for rev in self.src.db.execute(
                "select * from revlog where cid = ?", scid):
                rev = list(rev)
                rev[1] = card[0]
                rev[2] = self.dst.usn()
                revlog.append(rev)
            cnt += 1
        # apply
        self.dst.db.executemany("""
insert or ignore into cards values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", cards)
        self.dst.db.executemany("""
insert or ignore into revlog values (?,?,?,?,?,?,?,?,?)""", revlog)
        self.log.append(ngettext("%d card imported.", "%d cards imported.", cnt) % cnt)

    # Media
    ######################################################################

    def _importMedia(self):
        self.src.media.copyTo(self.dst.media.dir())

    # Post-import cleanup
    ######################################################################
    # fixme: we could be handling new card order more elegantly on import

    def _postImport(self):
        # make sure new position is correct
        self.dst.conf['nextPos'] = self.dst.db.scalar(
            "select max(due)+1 from cards where type = 0") or 0
        self.dst.save()
